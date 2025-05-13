import os
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import cv2
import jax
import numpy as np
import torch
import tyro
from array_util import stack_and_pad
from flax.traverse_util import flatten_dict
from jax import numpy as jnp
from rich.pretty import pprint
from util import (detect_humans, extract_hand_keypoints, infer, init_detector,
                  run_hand_reconstruction, timing)
from vitpose_model import ViTPoseModel
from webpolicy.deploy.base_policy import BasePolicy
from webpolicy.deploy.server import WebsocketPolicyServer as Server

from hamer.configs import CACHE_DIR_HAMER
from hamer.models import (DEFAULT_CHECKPOINT, HAMER, MANO, download_models,
                          load_hamer)
from hamer.utils import SkeletonRenderer, recursive_to
from hamer.utils.renderer import Renderer, cam_crop_to_full


def resize(img, size=(224, 224)):
    img = tf.image.resize(img, size=size, method="lanczos3", antialias=True)
    return tf.cast(tf.clip_by_value(tf.round(img), 0, 255), tf.uint8).numpy()


HAMER_STAT = {
    "mean": [0.485, 0.456, 0.406],
    "std": [0.229, 0.224, 0.225],
}


def unnormalize(img):
    """bring image back to 0-255 range"""
    img = img * np.array(HAMER_STAT["std"]) + np.array(HAMER_STAT["mean"])
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)

from typing import Optional, Tuple, Union, List

@dataclass
class Intrinsics:
    fx: float  # focal length in pixels
    fy: float  # focal length in pixels
    cx: float
    cy: float

    distortion: Optional[np.ndarray] = field(default_factory=lambda: np.zeros(5))

    def create(self):
        return (self.fx, self.fy, self.cx, self.cy)

    @property
    def mat(self) -> np.ndarray:
        return np.array(
            [
                [self.fx, 0, self.cx],
                [0, self.fy, self.cy],
                [0, 0, 1],
            ],
            dtype=np.float32,
        )


@dataclass
class Distortion:
    k1: float  # radial distortion
    k2: float  # radial distortion
    p1: float  # tangential distortion
    p2: float  # tangential distortion
    k3: float  # radial distortion

    def from_vector(self, vec: List[float]) -> "Distortion":
        assert len(vec) == 5, "Vector must have 5 elements"
        return Distortion(*vec)

    def create(self):
        return np.array([self.k1, self.k2, self.p1, self.p2, self.k3], dtype=np.float32)


# Default intrinsics (approximate for 720p MacBook webcam)
INTR_MAC = Intrinsics(fx=600.0, fy=600.0, cx=640 / 2, cy=480 / 2)
INTR_LOGITECH_LOW = Intrinsics(
    fx=517.24,
    fy=517.94,
    cx=323.61,
    cy=252.13,
    distortion=np.array([[0.075, -0.124, 0.0, -0.0, -0.081]]),
)


class Policy(BasePolicy):

    def __init__(self, cfg):
        self.cfg = cfg

        # Download and load checkpoints
        download_models(CACHE_DIR_HAMER)
        self.model, self.model_cfg = load_hamer(cfg.checkpoint)

        pprint(self.model_cfg)

        # Setup HaMeR model
        self.device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        self.model = self.model.to(self.device)
        self.model.eval()
        self.model = torch.compile(self.model, backend="inductor", mode="max-autotune")

        self.detector = init_detector(cfg)  # Load detector
        # self.detector.model = torch.compile(self.detector.model, backend="inductor")

        self.vitpose = ViTPoseModel(self.device)  # keypoint detector
        # self.vitpose.model = torch.compile(self.vitpose.model, backend="inductor")

        self.renderer = Renderer(
            self.model_cfg, faces=self.model.mano.faces
        )  # Setup the renderer
        self.skrenderer = SkeletonRenderer(self.model_cfg)

        print("policy init done")

        # self._infer = torch.compile(self._infer, backend="inductor")

        self.reset()

        _impath = Path(__file__).parent.parent / "example_data" / "test1.jpg"
        _im = cv2.imread(str(_impath))
        self.infer({"img": _im})

        self.reset()

    def reset(self):
        # frame caching for speed
        self.pred_bboxes, self.pred_scores = None, None
        self.bboxes, self.is_right = None, None
        self.every_human = 0
        self.every_hand = 0


    def _infer(
        self, img, detector, vitpose, device, model, model_cfg, renderer: Renderer, cfg
    ):

        img_path = "demo.jpg"
        img_cv2 = img.copy()[:, :, ::-1]  # RGB to BGR

        print("### 1. Detect humans with ViTDet")
        if self.every_human % self.cfg.every_det == 0:
            pred_bboxes, pred_scores = detect_humans(detector, img_cv2)
            self.pred_bboxes, self.pred_scores = pred_bboxes, pred_scores
            self.every_human += 1
        else:
            pred_bboxes, pred_scores = self.pred_bboxes, self.pred_scores
        self.every_human += 1

        print(f"Detected {len(pred_bboxes)} people but only using the first one")
        pred_bboxes = pred_bboxes[:1]
        pred_scores = pred_scores[:1]

        print("### 2. Detect coarse keypoints with ViTPose")

        if self.every_hand % self.cfg.every_pose == 0:
            pred_bboxes = pred_bboxes.cpu().numpy()
            pred_scores = pred_scores.cpu().numpy()
            poses = timing()(vitpose.predict_pose)(
                img,
                [np.concatenate([pred_bboxes, pred_scores[:, None]], axis=1)],
            )
            print("### 3. Extract hand boxes from ViTPose poses")
            bboxes, is_right = extract_hand_keypoints(poses)
            self.bboxes, self.is_right = bboxes, is_right
        else:
            bboxes, is_right = self.bboxes, self.is_right
        self.every_hand += 1

        # print(bboxes, is_right)

        if len(bboxes) == 0:
            print("No hands detected")
            return

        print("### 4. get MANO parameters from HaMeR")

        OUT, front = run_hand_reconstruction(
            model_cfg,
            img_cv2,
            bboxes,
            is_right,
            device,
            model,
            renderer,
            img_path,
            cfg,
        )

        # print(OUT.keys)
        return OUT

    def infer(self, obs: dict):

        if obs is None or 'img' not in obs or not isinstance(obs['img'], np.ndarray):
            return {}

        with torch.no_grad(), torch.cuda.amp.autocast(True, dtype=torch.float16):

            img = obs["img"]
            # img = img.contiguous(memory_format=torch.channels_last) # for CNN
            out = timing(label='infer')(self._infer)(
                img=img,
                detector=self.detector,
                vitpose=self.vitpose,
                device=self.device,
                model=self.model,
                model_cfg=self.model_cfg,
                renderer=self.renderer,
                cfg=self.cfg,
            )

        # everything is wrapped in list
        spec = lambda arr: jax.tree.map(lambda x: (type(x), x.shape), arr)
        is_leaf = lambda x: isinstance(x, (list, tuple))
        out = jax.tree.map(lambda x: x[0], out.data, is_leaf=is_leaf)
        out = flatten_dict(out, sep=".")

        clean = lambda x: (x.cpu().numpy() if isinstance(x, torch.Tensor) else x)
        out = jax.tree.map(clean, out)

        if not self.cfg.fast:
            pprint(spec(out))

        fns = [
                lambda x: x.transpose(1,2,0),
                lambda x: cv2.resize(x, (224,224))
                ]

        prepare = lambda x: x.transpose(1, 2, 0)
        out["img_wrist"] = np.stack([prepare(x) for x in out.pop("img")])
        out["img_wrist"] = unnormalize(out["img_wrist"])
        out["img"] = img

        return out


@dataclass
class PolicyConfig:

    fast: bool = True  # If set, use fast mode for inference, disable viz
    every_det: int = 5  # every n frames to run non-hamer detectors
    every_pose: int = 2  # every n frames to run non-hamer detectors

    checkpoint: str = DEFAULT_CHECKPOINT  # Path to pretrained model checkpoint
    img_folder: str = "example_data"  # Folder with input images
    out_folder: str = "out_demo"  # Output folder to save rendered results
    side_view: bool = False  # If set, render side view also
    full_frame: bool = True  # If set, render all people together also
    save_mesh: bool = False  # If set, save meshes to disk also
    batch_size: int = 1  # Batch size for inference/fitting
    rescale_factor: float = 2.0  # Factor for padding the bbox
    body_detector: str = "vitdet"  # Using regnety improves runtime and reduces memory
    file_type: List[str] = field(default_factory=lambda: ["*.jpg", "*.png"])
    device: int = 0  # Cuda device to run the server on


@dataclass
class Config:
    """config node for demo"""

    policy: PolicyConfig = field(default_factory=PolicyConfig)

    port: int = 8002  # Port to run the server on
    host: str = "0.0.0.0"  # Host to run the server on


def main(cfg: Config):

    pprint(cfg)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(cfg.policy.device)

    policy = Policy(cfg.policy)
    server = Server(
        policy,
        host=cfg.host,
        port=cfg.port,
        metadata=None,
    )
    print("serving on", cfg.host, cfg.port)
    server.serve_forever()


if __name__ == "__main__":
    main(tyro.cli(Config))
