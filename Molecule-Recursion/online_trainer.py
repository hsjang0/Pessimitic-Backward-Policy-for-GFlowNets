import copy
import os
import pathlib

import git
import torch
from omegaconf import OmegaConf
from torch import Tensor

from gflownet.algo.advantage_actor_critic import A2C
from gflownet.algo.flow_matching import FlowMatching
from gflownet.algo.soft_q_learning import SoftQLearning
from gflownet.algo.trajectory_balance import TrajectoryBalance
from gflownet.algo.pbp_tb import PBP_TB
from gflownet.algo.gafn import GAFN
from gflownet.data.replay_buffer import ReplayBuffer
from gflownet.models.graph_transformer import GraphTransformerGFN

from .trainer import GFNTrainer


def model_grad_norm(model):
    x = 0
    for i in model.parameters():
        if i.grad is not None:
            x += (i.grad * i.grad).sum()
    return torch.sqrt(x)


class StandardOnlineTrainer(GFNTrainer):
    def setup_model(self):
        self.model = GraphTransformerGFN(
            self.ctx,
            self.cfg,
            do_bck=self.cfg.algo.tb.do_parameterize_p_b,
            num_graph_out=self.cfg.algo.tb.do_predict_n + 1,
        )

    def setup_algo(self):
        algo = self.cfg.algo.method
        if algo == "TB":
            algo = TrajectoryBalance
        elif algo == "PBP_TB":
            algo = PBP_TB
        elif algo == "FM":
            algo = FlowMatching
        elif algo == "A2C":
            algo = A2C
        elif algo == "SQL":
            algo = SoftQLearning
        elif algo == "GAFN":
            algo = GAFN
        else:
            raise ValueError(algo)
        self.algo = algo(self.env, self.ctx, self.rng, self.cfg)

    def setup_data(self):
        self.training_data = []
        self.test_data = []

    def _opt(self, params, lr=None, momentum=None):
        if lr is None:
            lr = self.cfg.opt.learning_rate
        if momentum is None:
            momentum = self.cfg.opt.momentum
        if self.cfg.opt.opt == "adam":
            return torch.optim.Adam(
                params,
                lr,
                (momentum, 0.999),
                weight_decay=self.cfg.opt.weight_decay,
                eps=self.cfg.opt.adam_eps,
            )

        raise NotImplementedError(f"{self.cfg.opt.opt} is not implemented")

    def setup(self):
        super().setup()
        self.offline_ratio = 0
        self.replay_buffer = ReplayBuffer(self.cfg, self.rng) if self.cfg.replay.use else None

        # Separate Z parameters from non-Z to allow for LR decay on the former
        if hasattr(self.model, "logZ"):
            Z_params = list(self.model.logZ.parameters())
            non_Z_params = [i for i in self.model.parameters() if all(id(i) != id(j) for j in Z_params)]
        else: 
            Z_params = []
            non_Z_params = list(self.model.parameters())
        
        self.opt = self._opt(non_Z_params)
        self.opt_b = torch.optim.Adam(
                non_Z_params,
                1e-3,
                weight_decay=self.cfg.opt.weight_decay,
                eps=self.cfg.opt.adam_eps,
            )
        
        self.opt_Z = self._opt(Z_params, self.cfg.algo.tb.Z_learning_rate, 0.9)
        self.lr_sched = torch.optim.lr_scheduler.LambdaLR(self.opt, lambda steps: 2 ** (-steps / self.cfg.opt.lr_decay))
        self.lr_sched_b = torch.optim.lr_scheduler.LambdaLR(self.opt_b, lambda steps: 2 ** (-steps / self.cfg.opt.lr_decay))
        self.lr_sched_Z = torch.optim.lr_scheduler.LambdaLR(
            self.opt_Z, lambda steps: 2 ** (-steps / self.cfg.algo.tb.Z_lr_decay)
        )

        self.sampling_tau = self.cfg.algo.sampling_tau
        if self.sampling_tau > 0:
            self.sampling_model = copy.deepcopy(self.model)
        else:
            self.sampling_model = self.model

        self.mb_size = self.cfg.algo.global_batch_size
        self.clip_grad_callback = {
            "value": lambda params: torch.nn.utils.clip_grad_value_(params, self.cfg.opt.clip_grad_param),
            "norm": lambda params: [torch.nn.utils.clip_grad_norm_(p, self.cfg.opt.clip_grad_param) for p in params],
            "total_norm": lambda params: torch.nn.utils.clip_grad_norm_(params, self.cfg.opt.clip_grad_param),
            "none": lambda x: None,
        }[self.cfg.opt.clip_grad_type]

        # saving hyperparameters
        #git_hash = git.Repo(__file__, search_parent_directories=True).head.object.hexsha[:7]
        #self.cfg.git_hash = git_hash

        yaml_cfg = OmegaConf.to_yaml(self.cfg)
        if self.print_config:
            print("\n\nHyperparameters:\n")
            print(yaml_cfg)
        os.makedirs(self.cfg.log_dir, exist_ok=True)
        with open(pathlib.Path(self.cfg.log_dir) / "config.yaml", "w", encoding="utf8") as f:
            f.write(yaml_cfg)

    def step(self, loss: Tensor):
        loss.backward()
        with torch.no_grad():
            g0 = model_grad_norm(self.model)
            self.clip_grad_callback(self.model.parameters())
            g1 = model_grad_norm(self.model)
        self.opt.step()
        self.opt.zero_grad()
        self.opt_Z.step()
        self.opt_Z.zero_grad()
        self.lr_sched.step()
        self.lr_sched_Z.step()
        if self.sampling_tau > 0:
            for a, b in zip(self.model.parameters(), self.sampling_model.parameters()):
                b.data.mul_(self.sampling_tau).add_(a.data * (1 - self.sampling_tau))
        return {"grad_norm": g0, "grad_norm_clip": g1}


    def step_pbp(self, loss: Tensor):
        loss.backward()
        self.opt_b.step()
        self.opt_b.zero_grad()
        return None