import torch
import torch.nn.functional as F
from SESS.cam import BaseCAM


class GradCAM(BaseCAM):
    def __init__(self, model, target_layer="module.layer4.2"):
        super().__init__(model, target_layer)

    def forward(self, x, class_idx=None, retain_graph=False):
        if len(x.size()) == 3:
            x = x.unsqueeze(0)

        x = x.to(next(self.model.parameters()).device)
        b, c, h, w = x.size()

        # predication on raw x
        logit = self.model(x)
        softmax = F.softmax(logit, dim=1)

        if class_idx is None:
            score = logit[:, logit.max(1)[-1]]#.squeeze()
        else:
            score = logit[:, class_idx]#.squeeze()
            # score = logit[:, class_idx]

        if b > 1:
            retain_graph = True

        self.model.zero_grad()
        gradients_list = []
        for i, item in enumerate(score):
            item.backward(retain_graph=retain_graph)
            gradients = self.gradients['value'].data[i]
            gradients_list.append(gradients)

        gradients = torch.stack(gradients_list, dim=0)
        activations = self.activations['value'].data

        if 'swin' in str(self.model.__class__).lower():
           activations = activations.view(activations.shape[0],7,7,activations.shape[-1]) 
           activations = torch.permute(activations,(0,3,1,2))
           gradients = gradients.view(gradients.shape[0],7,7,gradients.shape[-1]) 
           gradients = torch.permute(gradients,(0,3,1,2))
        elif 'vit' in str(self.model.__class__).lower():
           activations = activations[:,1:,:]
           gradients = gradients[:,1:,:]
           activations = activations.view(activations.shape[0],14,14,activations.shape[-1]) 
           activations = torch.permute(activations,(0,3,1,2))
           gradients = gradients.view(gradients.shape[0],14,14,gradients.shape[-1]) 
           gradients = torch.permute(gradients,(0,3,1,2))        

       
        b, k, u, v = activations.size()

        alpha = gradients.view(b, k, -1).mean(2)
        weights = alpha.view(b, k, 1, 1)
        saliency_map = (weights * activations).sum(1, keepdim=True)

        saliency_map = F.relu(saliency_map)
        saliency_map = F.interpolate(saliency_map, size=(h, w), mode='bilinear', align_corners=False)
        # saliency_map_min, saliency_map_max = saliency_map.min(), saliency_map.max()
        # saliency_map = (saliency_map - saliency_map_min) / (saliency_map_max - saliency_map_min)

        saliency_map_shape = saliency_map.shape
        saliency_map = saliency_map.view(saliency_map.shape[0], -1)
        saliency_map_min, saliency_map_max = saliency_map.min(1, keepdim=True)[0], saliency_map.max(1, keepdim=True)[0]
        saliency_map = (saliency_map - saliency_map_min) / (saliency_map_max - saliency_map_min)
        saliency_map = saliency_map.view(saliency_map_shape)

        # import cv2
        # import numpy as np
        # map = saliency_map.cpu().data
        # map = cv2.applyColorMap(np.uint8(255 * map.squeeze()), cv2.COLORMAP_JET)
        # cv2.imwrite('test.jpg', map)

        return saliency_map.detach().cpu().numpy(), softmax.detach()

    def __call__(self, x, class_idx=None, retain_graph=False):
        return self.forward(x, class_idx, retain_graph)


class GradCAMpp(BaseCAM):
    def __init__(self, model, target_layer="module.layer4.2"):
        super().__init__(model, target_layer)

    def forward(self, x, class_idx=None, retain_graph=False):
        b, c, h, w = x.size()

        # predication on raw x
        logit = self.model(x)

        if class_idx is None:
            score = logit[:, logit.max(1)[-1]].squeeze()
        else:
            score = logit[:, class_idx].squeeze()

        self.model.zero_grad()
        score.backward(retain_graph=retain_graph)
        gradients = self.gradients['value']  # dS/dA
        activations = self.activations['value']  # A
        b, k, u, v = activations.size()

        alpha_num = gradients.pow(2)
        alpha_denom = gradients.pow(2).mul(2) + \
                      activations.mul(gradients.pow(3)).view(b, k, u * v).sum(-1, keepdim=True).view(b, k, 1, 1)
        alpha_denom = torch.where(alpha_denom != 0.0, alpha_denom, torch.ones_like(alpha_denom))
        alpha = alpha_num.div(alpha_denom + 1e-7)
        positive_gradients = F.relu(score.exp() * gradients)  # ReLU(dY/dA) == ReLU(exp(S)*dS/dA))
        weights = (alpha * positive_gradients).view(b, k, u * v).sum(-1).view(b, k, 1, 1)

        saliency_map = (weights * activations).sum(1, keepdim=True)
        saliency_map = F.relu(saliency_map)
        saliency_map = F.interpolate(saliency_map, size=(h, w), mode='bilinear', align_corners=False)
        saliency_map_min, saliency_map_max = saliency_map.min(), saliency_map.max()
        saliency_map = (saliency_map - saliency_map_min) / (saliency_map_max - saliency_map_min).data

        return saliency_map

    def __call__(self, x, class_idx=None, retain_graph=False):
        return self.forward(x, class_idx, retain_graph)


class SmoothGradCAM(BaseCAM):
    def __init__(self, model, target_layer="module.layer4.2", stdev_spread=0.15, n_samples=20, magnitude=True):
        super().__init__(model, target_layer)
        self.stdev_spread = stdev_spread
        self.n_samples = n_samples
        self.magnitude = magnitude

    def forward(self, x, class_idx=None, retain_graph=False):
        b, c, h, w = x.size()

        if class_idx is None:
            predicted_class = self.model(x).max(1)[-1]
        else:
            predicted_class = torch.LongTensor([class_idx])

        saliency_map = 0.0

        stdev = self.stdev_spread / (x.max() - x.min())
        std_tensor = torch.ones_like(x) * stdev

        self.model.zero_grad()
        for i in range(self.n_samples):
            x_plus_noise = torch.normal(mean=x, std=std_tensor)
            x_plus_noise.requires_grad_()
            x_plus_noise.cuda()
            logit = self.model(x_plus_noise)
            score = logit[0][predicted_class]
            score.backward(retain_graph=True)

            gradients = self.gradients['value']
            if self.magnitude:
                gradients = gradients * gradients
            activations = self.activations['value']
            b, k, u, v = activations.size()

            alpha = gradients.view(b, k, -1).mean(2)
            weights = alpha.view(b, k, 1, 1)

            saliency_map += (weights * activations).sum(1, keepdim=True).data

        saliency_map = F.relu(saliency_map)
        saliency_map = F.interpolate(saliency_map, size=(h, w), mode='bilinear', align_corners=False)
        saliency_map_min, saliency_map_max = saliency_map.min(), saliency_map.max()
        saliency_map = (saliency_map - saliency_map_min) / (saliency_map_max - saliency_map_min).data

        return saliency_map

    def __call__(self, x, class_idx=None, retain_graph=False):
        return self.forward(x, class_idx, retain_graph)
