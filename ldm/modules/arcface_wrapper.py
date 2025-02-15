import torch
import torch.nn as nn
import torch.nn.functional as F
import cv2
import numpy as np
from evaluation.arcface_resnet import resnet_face18
from evaluation.retinaface_pytorch import RetinaFaceClient

def load_image_for_arcface(img_path, device='cpu'):
    # cv2.imread ignores the alpha channel by default.
    image = cv2.imread(img_path)
    if image is None:
        return None
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = image.transpose((2, 0, 1))
    image = image[np.newaxis, :, :, :]    
    image = image.astype(np.float32, copy=False)
    # Normalize to [-1, 1].
    image -= 127.5
    image /= 127.5
    image_ts = torch.from_numpy(image).to(device)
    return image_ts

class ArcFaceWrapper(nn.Module):
    def __init__(self, device='cpu', dtype=torch.float16, ckpt_path='models/arcface-resnet18_110.pth'):
        super(ArcFaceWrapper, self).__init__()
        self.arcface = resnet_face18(False)
        ckpt_state_dict = torch.load(ckpt_path, map_location='cpu')
        for key in list(ckpt_state_dict.keys()):
            new_key = key.replace("module.", "")
            ckpt_state_dict[new_key] = ckpt_state_dict.pop(key)

        self.arcface.load_state_dict(ckpt_state_dict)
        self.arcface.eval()
        self.dtype = dtype
        self.arcface.to(device, dtype=self.dtype)

        self.retinaface = RetinaFaceClient(device=device)
        # We keep retinaface at float32, as it doesn't require grad and won't consume much memory.
        self.retinaface.eval()

        for param in self.arcface.parameters():
            param.requires_grad = False
        for param in self.retinaface.parameters():
            param.requires_grad = False

    # Suppose images_ts has been normalized to [-1, 1].
    # Cannot wrap this function with @torch.compile. Otherwise a lot of warnings will be spit out.
    def embed_image_tensor(self, images_ts, T=20, bleed=0, embed_bg_faces=True,
                           use_whole_image_if_no_face=False, enable_grad=True):
        # retina_crop_face() crops on the input tensor, so that computation graph w.r.t. 
        # the input tensor is preserved.
        # But the cropping operation is wrapped with torch.no_grad().
        # fg_face_bboxes: long tensor of [BS1, 4], BS1: the number of successful instances in the batch.
        fg_face_crops, bg_face_crops_flat, fg_face_bboxes, failed_inst_indices = \
            self.retinaface.crop_faces(images_ts, out_size=(128, 128), T=T, bleed=bleed,
                                       use_whole_image_if_no_face=use_whole_image_if_no_face)
        
        # No face detected in any instances in the batch. fg_face_bboxes is an empty tensor.
        if fg_face_crops is None:
            return None, None, None, failed_inst_indices
        
        # Arcface takes grayscale images as input
        rgb_to_gray_weights = torch.tensor([0.299, 0.587, 0.114], device=images_ts.device).view(1, 3, 1, 1)
        # Convert RGB to grayscale
        fg_faces_gray = (fg_face_crops * rgb_to_gray_weights).sum(dim=1, keepdim=True)
        # Resize to (128, 128); arcface takes 128x128 images as input.
        fg_faces_gray = F.interpolate(fg_faces_gray, size=(128, 128), mode='bilinear', align_corners=False)
        with torch.set_grad_enabled(enable_grad):
            fg_faces_emb = self.arcface(fg_faces_gray.to(self.dtype))

        if embed_bg_faces and bg_face_crops_flat is not None:
            bg_faces_gray = (bg_face_crops_flat * rgb_to_gray_weights).sum(dim=1, keepdim=True)
            bg_faces_gray = F.interpolate(bg_faces_gray, size=(128, 128), mode='bilinear', align_corners=False)
            with torch.set_grad_enabled(enable_grad):
                bg_faces_emb = self.arcface(bg_faces_gray.to(self.dtype))
        else:
            bg_faces_emb = None

        return fg_faces_emb, bg_faces_emb, fg_face_bboxes, failed_inst_indices

    # T: minimal face height/width to be detected.
    # ref_images:     the groundtruth images.
    # aligned_images: the generated   images.
    def calc_arcface_align_loss(self, ref_images, aligned_images, T=20, bleed=2, 
                                suppress_bg_faces=True,
                                use_whole_image_if_no_face=False):
        # ref_fg_face_bboxes: long tensor of [BS, 4], where BS is the batch size.
        ref_fg_faces_emb, _, ref_fg_face_bboxes, ref_failed_inst_indices = \
            self.embed_image_tensor(ref_images, T, bleed, embed_bg_faces=False,
                                    use_whole_image_if_no_face=False, enable_grad=False)
        # bg_embs are not separated by instances, but flattened. 
        # We don't align them, just suppress them. So we don't need the batch dimension.
        aligned_fg_faces_emb, aligned_bg_faces_emb, aligned_fg_face_bboxes, aligned_failed_inst_indices = \
            self.embed_image_tensor(aligned_images, T, bleed, embed_bg_faces=suppress_bg_faces,
                                    use_whole_image_if_no_face=use_whole_image_if_no_face, 
                                    enable_grad=True)
        
        zero_losses = [ torch.tensor(0., dtype=ref_images.dtype, device=ref_images.device) for _ in range(2) ]
        if len(ref_failed_inst_indices) > 0:
            print(f"Failed to detect faces in ref_images-{ref_failed_inst_indices}")
            return zero_losses[0], zero_losses[1], None
        if len(aligned_failed_inst_indices) > 0:
            print(f"Failed to detect faces in aligned_images-{aligned_failed_inst_indices}")
            return zero_losses[0], zero_losses[1], None

        # If the numbers of instances in ref_fg_faces_emb and aligned_fg_faces_emb are different, then there's only one ref image, 
        # and multiple aligned images of the same person.
        # We repeat groundtruth embeddings to match the number of generated embeddings.
        if len(ref_fg_faces_emb) < len(aligned_fg_faces_emb):
            ref_fg_faces_emb = ref_fg_faces_emb.repeat(len(aligned_fg_faces_emb)//len(ref_fg_faces_emb), 1)
        
        # labels = 1: align the embeddings of the same person.
        loss_arcface_align = F.cosine_embedding_loss(ref_fg_faces_emb, aligned_fg_faces_emb, torch.ones(ref_fg_faces_emb.shape[0]).to(ref_fg_faces_emb.device))
        print(f"loss_arcface_align: {loss_arcface_align.item():.2f}")

        if suppress_bg_faces and aligned_bg_faces_emb is not None:
            # Suppress background faces by pushing their embeddings towards zero.
            loss_bg_faces_suppress = (aligned_bg_faces_emb**2).mean()
            print(f"loss_bg_faces_suppress: {loss_bg_faces_suppress.item():.2f}")
        else:
            if suppress_bg_faces and aligned_bg_faces_emb is None:
                print("loss_bg_faces_suppress = 0. No background faces detected in aligned_images. ")

            loss_bg_faces_suppress = zero_losses[0]

        return loss_arcface_align, loss_bg_faces_suppress, aligned_fg_face_bboxes
