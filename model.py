import torch
import torch.nn as nn
import torch.nn.functional as F
from distributions import Categorical, DiagGaussian
from utils import orthogonal, att, temporal_att

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1 or classname.find('Linear') != -1:
        orthogonal(m.weight.data)
        if m.bias is not None:
            m.bias.data.fill_(0)


class FFPolicy(nn.Module):
    def __init__(self):
        super(FFPolicy, self).__init__()

    def forward(self, inputs, states, masks):
        raise NotImplementedError

    def act(self, inputs, states, masks, deterministic=False):
        value, x, states = self(inputs, states, masks)
        action = self.dist.sample(x, deterministic=deterministic)
        action_log_probs, dist_entropy = self.dist.logprobs_and_entropy(x, action)
        return value, action, action_log_probs, states

    def evaluate_actions(self, inputs, states, masks, actions):
        value, x, states = self(inputs, states, masks)
        action_log_probs, dist_entropy = self.dist.logprobs_and_entropy(x, actions)
        return value, action_log_probs, dist_entropy, states


class CNNPolicy(FFPolicy):
    def __init__(self, num_inputs, action_space, use_gru, use_att):
        super(CNNPolicy, self).__init__()
        self.conv1 = nn.Conv2d(num_inputs, 32, 8, stride=4)
        self.conv2 = nn.Conv2d(32, 64, 4, stride=2)

        if use_att == 'spatial':
            #print('spatial')
            self.conv3 = nn.Conv2d(64, 256, 3, stride=1)
        else:
            self.conv3 = nn.Conv2d(64, 32, 3, stride=1)


        self.linear1 = nn.Linear(32 * 7 * 7, 512)

        if use_att == 'spatial':
            #print('spatial')
            self.att = att(256, 256)
        elif use_att == 'temporal':
            #print('temporal')
            self.att = temporal_att(256, 256)

        if use_gru:
            if use_att == 'spatial':
                #print('spatial')
                self.gru = nn.GRUCell(256, 256)
            else:
                self.gru = nn.GRUCell(512, 256)


        self.critic_linear = nn.Linear(256, 1)

        if action_space.__class__.__name__ == "Discrete":
            # HARCODED CHAGING
            num_outputs = action_space.n
            self.dist = Categorical(256, num_outputs)
        elif action_space.__class__.__name__ == "Box":
            #print("Sampling from Box")
            num_outputs = action_space.shape[0]
            self.dist = DiagGaussian(256, num_outputs)
        else:
            raise NotImplementedError

        self.train()
        self.reset_parameters()
        self.use_att = use_att

    @property
    def state_size(self):
        if hasattr(self, 'gru'):
            return 256
        else:
            return 1

    def reset_parameters(self):
        self.apply(weights_init)

        relu_gain = nn.init.calculate_gain('relu')
        self.conv1.weight.data.mul_(relu_gain)
        self.conv2.weight.data.mul_(relu_gain)
        self.conv3.weight.data.mul_(relu_gain)
        self.linear1.weight.data.mul_(relu_gain)

        if hasattr(self, 'gru'):
            orthogonal(self.gru.weight_ih.data)
            orthogonal(self.gru.weight_hh.data)
            self.gru.bias_ih.data.fill_(0)
            self.gru.bias_hh.data.fill_(0)

        if self.dist.__class__.__name__ == "DiagGaussian":
            self.dist.fc_mean.weight.data.mul_(0.01)

    def forward(self, inputs, states, masks):
        x = self.conv1(inputs / 255.0)
        x = F.relu(x)

        x = self.conv2(x)
        x = F.relu(x)

        x = self.conv3(x)
        x = F.relu(x)

        if self.use_att == 'spatial':
            #print("GO FOR ATTENTION","RECEIVEING FROM CONVOLUTION THIS ONE", x.size())
            x = x.view(-1, 49, 256)
        else:
            x = x.view(-1, 32 * 7 * 7)
            x = self.linear1(x)
            x = F.relu(x)

        if hasattr(self, 'gru'):
            if inputs.size(0) == states.size(0):
                if self.use_att == 'spatial':
                    #print("I AMMMM PAYING ATTENTION")
                    #print("BEFORE ATTEND",x.size())
                    x = self.att(x, states*masks)
                    #print("AFTER ATTEND",x.size())
                    #print(x)
                    x = states = self.gru(x, states * masks)

                else:
                    x = states = self.gru(x, states * masks)
            else:

                if self.use_att == 'spatial':
                    #print('spatial')
                    x = x.view(-1, states.size(0), 49, 256)
                    masks = masks.view(-1, states.size(0) , 1)
                else:
                    x = x.view(-1, states.size(0), x.size(1))
                    masks = masks.view(-1, states.size(0), 1)

                outputs = []

                for i in range(x.size(0)):
                    if self.use_att == 'spatial':
                        #print('spatial')
                        X = self.att(x[i], states*masks[i])
                        hx = states = self.gru(X, states * masks[i])
                        outputs.append(hx)
                    else:
                        hx = states = self.gru(x[i], states * masks[i])
                        outputs.append(hx)

                x = torch.cat(outputs, 0)

            if self.use_att == "temporal":
                #print('X_ before attention', x.size())
                x = self.att(x)
                #print('X_after attention', x.size())
                
        return self.critic_linear(x), x, states


def weights_init_mlp(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
        m.weight.data.normal_(0, 1)
        m.weight.data *= 1 / torch.sqrt(m.weight.data.pow(2).sum(1, keepdim=True))
        if m.bias is not None:
            m.bias.data.fill_(0)


class MLPPolicy(FFPolicy):
    def __init__(self, num_inputs, action_space):
        super(MLPPolicy, self).__init__()

        self.action_space = action_space

        self.a_fc1 = nn.Linear(num_inputs, 64)
        self.a_fc2 = nn.Linear(64, 64)

        self.v_fc1 = nn.Linear(num_inputs, 64)
        self.v_fc2 = nn.Linear(64, 64)
        self.v_fc3 = nn.Linear(64, 1)

        if action_space.__class__.__name__ == "Discrete":
            num_outputs = action_space.n
            self.dist = Categorical(64, num_outputs)
        elif action_space.__class__.__name__ == "Box":
            num_outputs = action_space.shape[0]
            self.dist = DiagGaussian(64, num_outputs)
        else:
            raise NotImplementedError

        self.train()
        self.reset_parameters()

    @property
    def state_size(self):
        return 1

    def reset_parameters(self):
        self.apply(weights_init_mlp)

        """
        tanh_gain = nn.init.calculate_gain('tanh')
        self.a_fc1.weight.data.mul_(tanh_gain)
        self.a_fc2.weight.data.mul_(tanh_gain)
        self.v_fc1.weight.data.mul_(tanh_gain)
        self.v_fc2.weight.data.mul_(tanh_gain)
        """

        if self.dist.__class__.__name__ == "DiagGaussian":
            self.dist.fc_mean.weight.data.mul_(0.01)

    def forward(self, inputs, states, masks):
        x = self.v_fc1(inputs)
        x = F.tanh(x)

        x = self.v_fc2(x)
        x = F.tanh(x)

        x = self.v_fc3(x)
        value = x

        x = self.a_fc1(inputs)
        x = F.tanh(x)

        x = self.a_fc2(x)
        x = F.tanh(x)

        return value, x, states
