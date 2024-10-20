import torch
from torchvision.models.optical_flow import raft_large
import cv2
import numpy as np
import torch.nn.functional as F
from .network import GMA
from .utils.utils import load_checkpoint as gma_load_checkpoint

def backward_warp_by_flow(image2, flow1to2):
    H, W, _ = image2.shape
    flow1to2 = flow1to2.copy()
    flow1to2[:, :, 0] += np.arange(W)  # Adjust x-coordinates
    flow1to2[:, :, 1] += np.arange(H)[:, None]  # Adjust y-coordinates
    image1_recovered = cv2.remap(image2, flow1to2, None, cv2.INTER_LINEAR)
    return image1_recovered

#model = raft_large(pretrained=True, progress=False).to('cuda')
#model = model.eval()
flow_model_config = { 'mixed_precision': True }
model = GMA(flow_model_config).to('cuda')
flow_model_ckpt_path = "models/gma-kitti.pth"
gma_load_checkpoint(model, flow_model_ckpt_path)

img1 = cv2.imread('gma/examples/1.png')
img2 = cv2.imread('gma/examples/2.png')
img1_batch = torch.from_numpy(img1).permute(2, 0, 1).unsqueeze(0).float().to('cuda')
img2_batch = torch.from_numpy(img2).permute(2, 0, 1).unsqueeze(0).float().to('cuda')
img1_batch = F.interpolate(img1_batch, scale_factor=4, mode='bilinear', align_corners=False)
img2_batch = F.interpolate(img2_batch, scale_factor=4, mode='bilinear', align_corners=False)

flow, flow_predictions = model(img1_batch, img2_batch, test_mode=1)
flow = F.interpolate(flow_predictions[-1].unsqueeze(0), scale_factor=0.25, mode='bilinear', align_corners=False) * 0.25
flow = flow.permute(0, 2, 3, 1)
img2_recovered = backward_warp_by_flow(img1, -flow[0].detach().cpu().numpy())
cv2.imwrite('gma/examples/2_recovered.png', img2_recovered)

