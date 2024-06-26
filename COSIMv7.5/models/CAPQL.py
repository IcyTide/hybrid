import copy

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
from torch.distributions.categorical import Categorical

from replay_memory import *


def weights_init_(m):
    if isinstance(m, nn.Linear):
        torch.nn.init.xavier_uniform_(m.weight, gain=1)
        torch.nn.init.constant_(m.bias, 0)


class Actor(nn.Module):

    def __init__(self, s_dim, out_c, out_d, wt_dim):
        super(Actor, self).__init__()

        self.linear1 = nn.Linear(s_dim + wt_dim, 128)
        self.linear2 = nn.Linear(128, 128)

        self.pi_d = nn.Linear(128, out_d)
        self.mean_linear = nn.Linear(128, out_c)
        self.log_std_linear = nn.Linear(128, out_c)

        self.sigmoid = nn.Sigmoid()
        self.apply(weights_init_)

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def forward(self, state, w):
        state_comp = torch.cat((state, w), dim=1)
        mask = torch.isnan(state_comp).any(dim=1)
        state_comp = state_comp[~mask]
        x = F.relu(self.linear1(state_comp))
        x = F.relu(self.linear2(x))

        pi_d = self.pi_d(x)
        mean = self.mean_linear(x)
        log_std = self.log_std_linear(x)

        log_std = torch.clamp(log_std, min=-20, max=2)
        return pi_d, mean, log_std

    def sample(self, state, w, num_device, num_server):
        state = torch.FloatTensor(state).to(self.device) if not torch.is_tensor(state) else state
        w = torch.FloatTensor(w).to(self.device) if not torch.is_tensor(w) else w
        pi_d, mean, log_std = self.forward(state, w)
        std = log_std.exp()
        normal = Normal(mean, std)
        x_t = normal.rsample()  # for reparameterization trick (mean + std * N(0,1))

        # restrict the outputs for continuous actions
        action_c_full = torch.sigmoid(x_t)
        log_prob_c_full = normal.log_prob(x_t)
        log_prob_c_full -= torch.log(1.0 - action_c_full.pow(2) + 1e-8)

        actions_d = []
        probs_d = []
        log_probs_d = []
        selected_action_c = []
        selected_log_prob_c = []

        for i in range(num_device):
            start_idx = i * (num_server + 1)
            end_idx = start_idx + num_server + 1

            dist = Categorical(logits=pi_d[:, start_idx:end_idx])
            action_dis = dist.sample()
            prob_d = dist.probs
            log_prob_d = torch.log(prob_d + 1e-8)

            actions_d.append(action_dis)
            probs_d.append(prob_d)
            log_probs_d.append(log_prob_d)

            action_c_start_idxs = i * (1 + num_server) * 2 + action_dis * 2
            action_c_end_idxs = action_c_start_idxs + 2
            selected_action_c_batch = torch.stack([action_c_full[i, start:end] for i, (start, end) in
                                                   enumerate(zip(action_c_start_idxs, action_c_end_idxs))])
            selected_log_prob_c_batch = torch.stack([log_prob_c_full[i, start:end] for i, (start, end) in
                                                     enumerate(zip(action_c_start_idxs, action_c_end_idxs))])
            selected_action_c.append(selected_action_c_batch)
            selected_log_prob_c.append(selected_log_prob_c_batch)

        actions_2d = [action.unsqueeze(1) if action.dim() == 1 else action for action in actions_d]
        prob_d_2d = [prob_d.unsqueeze(1) if prob_d.dim() == 1 else prob_d for prob_d in probs_d]
        log_prob_d_2d = [log_prob_d.unsqueeze(1) if log_prob_d.dim() == 1 else log_prob_d for log_prob_d in log_probs_d]
        action_d = torch.cat(actions_2d, dim=1)  # tensor[[server1, server2, local]]
        prob_d = torch.cat(prob_d_2d, dim=1)
        log_prob_d = torch.cat(log_prob_d_2d, dim=1)

        action_c = torch.cat(selected_action_c, dim=1)
        log_prob_c = torch.cat(selected_log_prob_c, dim=1)

        log_prob_c_full = torch.cat(
            [log_prob_c_full[:, i:i + 2].sum(1, keepdim=True) for i in range(0, 2 * num_device * (num_server + 1), 2)],
            dim=1)

        return action_c_full, log_prob_c_full, action_c, action_d, log_prob_c, log_prob_d, prob_d

    def to(self, device):
        return super(Actor, self).to(device)


class QNetwork(nn.Module):
    def __init__(self, state_dim, numberOfServer, numberOfDevice, wt_dim):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.numberOfServer = numberOfServer
        self.numberOfDevice = numberOfDevice
        out_c = numberOfDevice * 2
        out_d = (numberOfServer + 1) * numberOfDevice
        super(QNetwork, self).__init__()
        self.Q1 = nn.Sequential(
            nn.Linear(state_dim + out_c + out_d + wt_dim, 128),
            nn.ReLU(),
            nn.Linear(128, 128),
            nn.ReLU(),
            nn.Linear(128, wt_dim)
        )

        self.Q2 = copy.deepcopy(self.Q1)
        self.rwd_dim = wt_dim
        self.apply(weights_init_)

    def forward(self, state, action_c, action_d, w):  # 输入为选择后的离散动作和连续动作，离散动作=num_device，连续动作为2*num_device
        state = torch.FloatTensor(state).to(self.device) if not torch.is_tensor(state) else state
        action_c = torch.FloatTensor(action_c).to(self.device) if not torch.is_tensor(action_c) else action_c
        action_d = torch.Tensor(action_d).to(self.device) if not torch.is_tensor(action_d) else action_d.to(torch.int64)
        w = torch.FloatTensor(w).to(self.device) if not torch.is_tensor(w) else w

        # 将one-hot编码后的离散动作列沿着最后一个维度拼接起来
        one_hot_action_d_single = [F.one_hot(action_d[:, i], num_classes=self.numberOfServer + 1) for i in range(self.numberOfDevice)]
        one_hot_action_d = torch.cat(one_hot_action_d_single, dim=-1)

        combined_action_vectors = torch.empty((one_hot_action_d.size(0), 0))
        for i in range(self.numberOfDevice):  # 遍历每个设备的动作编码和连续参数
            one_hot_i = one_hot_action_d[:, i * (self.numberOfServer+1):(i + 1) * (self.numberOfServer+1)]
            action_c_i = action_c[:, i * 2:(i + 1) * 2]
            combined_i = torch.cat((one_hot_i, action_c_i), dim=1)
            combined_action_vectors = torch.cat((combined_action_vectors, combined_i), dim=1)
        # tensor([0.0000, 1.0000, 0.0000, 0.0808, 5.0516, 0.0000, 0.0000, 1.0000, 1.8706, 1.8764, 1.0000, 0.0000, 0.0000, 6.9715, 3.0237])

        x = torch.cat([state, combined_action_vectors, w], 1)
        x1 = self.Q1(x)
        x2 = self.Q2(x)
        return x1, x2
