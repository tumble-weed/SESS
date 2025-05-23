import torch
import torch.nn.functional as F
from kornia.filters.gaussian import gaussian_blur2d
from SESS.utils import group_sum
from SESS.cam import BaseCAM

blur = lambda x: gaussian_blur2d(x, kernel_size=(51, 51), sigma=(50., 50.))


class GroupCAM(BaseCAM):
    def __init__(self, model, target_layer="module.layer4.2", groups=32, cluster_method=None):
        super().__init__(model, target_layer)
        assert cluster_method in [None, 'k_means', 'agglomerate']
        self.cluster = cluster_method
        self.groups = groups

    def forward(self, x, class_idx=None, retain_graph=False):
        x = x.to(next(self.model.parameters()).device)
        b, c, h, w = x.size()
        logit = self.model(x)
        if logit.ndim == 4:
            logit = logit.mean(dim=(-1,-2))

        if class_idx is None:
            predicted_class = logit.max(1)[-1]
            score = logit[:, logit.max(1)[-1]].squeeze()
        else:
            predicted_class = torch.LongTensor([class_idx])
            score = logit[:, class_idx].squeeze()

        predicted_class = predicted_class.cuda()
        self.model.zero_grad()
        score.backward(retain_graph=retain_graph)
        gradients = self.gradients['value'].data
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
        activations = weights * activations

        if self.cluster is None:
            saliency_map = activations.chunk(self.groups, 1)
            # parallel implement
            saliency_map = torch.cat(saliency_map, dim=0)
            saliency_map = saliency_map.sum(1, keepdim=True)
        else:
            saliency_map = group_sum(activations, n=self.groups, cluster_method=self.cluster)
            saliency_map = torch.cat(saliency_map, dim=0)
        saliency_map = F.relu(saliency_map)
        saliency_map = F.interpolate(saliency_map, size=(h, w), mode='bilinear', align_corners=False)
        norm_saliency_map = saliency_map.reshape(self.groups, -1)
        inter_min = norm_saliency_map.min(dim=-1, keepdim=True)[0]
        inter_max = norm_saliency_map.max(dim=-1, keepdim=True)[0]
        inter_max[inter_max == inter_min] = 1
        norm_saliency_map = (norm_saliency_map-inter_min) / (inter_max - inter_min)
        #assert not norm_saliency_map.isnan().any()

        norm_saliency_map = norm_saliency_map.reshape(self.groups, 1, h, w)

        with torch.no_grad():
            _logit = self.model(blur(x).cuda())
            if len(_logit.shape) == 4:
                _logit = _logit[:, :, 0, 0]
            base_line = F.softmax(_logit, dim=-1)[0][predicted_class]
            blur_x = x * norm_saliency_map + blur(x) * (1 - norm_saliency_map)
            output = self.model(blur_x)
            if len(output.shape) == 4:
                output = output[:, :, 0, 0]

        output = F.softmax(output, dim=-1)
        # base_line = torch.squeeze(base_line).unsqueeze(0)
        # score = output[:, predicted_class] - base_line.unsqueeze(0).repeat(self.groups, 1)

        base_line = torch.squeeze(base_line)
        score = output[:, predicted_class] - base_line
        score = F.relu(score).unsqueeze(-1).unsqueeze(-1)
        # score = score.unsqueeze(-1).unsqueeze(-1)
        score_saliency_map = torch.sum(saliency_map * score, dim=0, keepdim=True)
        if score_saliency_map.isnan().any() or score_saliency_map.isinf().any():
            print('shouldnt be here')
            breakpoint()

 
        score_saliency_map_min, score_saliency_map_max = score_saliency_map.min(), score_saliency_map.max()
        if score_saliency_map_min == score_saliency_map_max:
            # return None
            return score_saliency_map.detach().cpu().numpy(), F.softmax(logit, dim=1).detach()

        score_saliency_map = (score_saliency_map - score_saliency_map_min) / (
                score_saliency_map_max - score_saliency_map_min).data
        if score_saliency_map.isnan().any() or score_saliency_map.isinf().any():
            print('shouldnt be here')
            breakpoint()

        return score_saliency_map.detach().cpu().numpy(), F.softmax(logit, dim=1).detach()

    def __call__(self, x, class_idx=None, retain_graph=False):
        return self.forward(x, class_idx, retain_graph)
