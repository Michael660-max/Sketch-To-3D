from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from io import BytesIO
from diffusers import (
    StableDiffusionControlNetPipeline,
    ControlNetModel,
    DPMSolverMultistepScheduler,
)
import torch
from controlnet_aux import HEDdetector
from contextlib import asynccontextmanager
import os
import numpy as np
import open3d as o3d
from torchvision import transforms
from timm.models import create_model
import uuid
import matplotlib.pyplot
import mediapipe as mp
from scipy.spatial import Delaunay


@asynccontextmanager
async def lifespan(app: FastAPI):
    # load the models once on startup
    if torch.cuda.is_available():
        app.state.device = torch.device("cuda")
        app.state.dtype = torch.float16
    elif torch.backends.mps.is_available():
        app.state.device = torch.device("mps")
        app.state.dtype = torch.float32
    else:
        app.state.device = torch.device("cpu")
        app.state.dtype = torch.float32

    # Stable Net + Diffusion
    app.state.hed = HEDdetector.from_pretrained("lllyasviel/Annotators")
    scribble = ControlNetModel.from_pretrained(
        "lllyasviel/sd-controlnet-scribble",
        torch_dtype=app.state.dtype,
        local_files_only=True,
    )

    pipe = StableDiffusionControlNetPipeline.from_pretrained(
        "runwayml/stable-diffusion-v1-5",
        controlnet=scribble,
        torch_dtype=app.state.dtype,
        local_files_only=True,
    )
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    pipe = pipe.to(app.state.device)
    pipe.enable_attention_slicing()
    pipe.enable_vae_slicing()
    if app.state.device.type == "cuda":
        pipe.enable_model_cpu_offload()
        pipe.enable_xformers_memory_efficient_attention()
    app.state.pipe = pipe

    # Mediapipe
    app.state.mp_face = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5,
    )

    yield


app = FastAPI(lifespan=lifespan)
tmp_dir = os.path.join(os.getcwd(), "tmp")
os.makedirs(tmp_dir, exist_ok=True)

app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/meshes", StaticFiles(directory=tmp_dir), name="meshes")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---Utiliy functions---
def estimate_depth(request: Request, img_path):
    """
    Generate a depth map of the image
    """
    device = request.app.state.device
    model = request.app.state.midas
    transform = request.app.state.midas_transform

    # tf = transforms.Compose(
    #     [
    #         transforms.Resize(256),
    #         transforms.ToTensor(),
    #         transforms.Normalize(
    #             mean=[0.485, 0.456, 0.406],
    #             std=[0.229, 0.224, 0.225],
    #         ),
    #     ]
    # )

    img = Image.open(img_path).convert("RGB")
    img_np = np.asarray(img)
    input_batch = transform(img_np).to(device)

    with torch.no_grad():
        depth = model(input_batch)

        # depth = (
        #     torch.nn.functional.interpolate(
        #         depth.unsqueeze(1),
        #         size=img.shape[::-1],
        #         mode="bicubic",
        #         align_corners=False,
        #     )
        #     .squeeze()
        #     .cpu()
        #     .numpy()
        # )

    depth = (
        torch.nn.functional.interpolate(
            depth.unsqueeze(1),
            size=img_np.shape[:2],  # (H, W)
            mode="bicubic",
            align_corners=False,
        )
        .squeeze()
        .cpu()
        .numpy()
    )

    return depth


# ---Endpoints---
@app.post("/generate_2d")
async def generate_image(request: Request, file: UploadFile = File(...)):
    contents = await file.read()
    form = await request.form()
    user_prompt = form.get("prompt")

    scribble_net = Image.open(BytesIO(contents)).convert("RGB").resize((512, 512))
    hed = request.app.state.hed
    pipe = request.app.state.pipe

    image = hed(scribble_net, scribble=True)

    prompt = ", ".join(
        [
            user_prompt,
            "photorealistic face portrait",
            "masterpiece, 8k resolution",
            "faithful interpretation of input line art",
            "natural eyes, nose, mouth, and ears",
            "soft even studio lighting, minimal shadows",
            "front-facing gaze, centered composition",
            "sharp details, realistic skin",
        ]
    )
    neg_prompt = ", ".join(
        [
            "blurry",
            "low-res",
            "cartoon",
            "deformed anatomy",
            "missing features",
            "artifacts",
            "poor lighting",
        ]
    )

    with torch.no_grad():
        result = pipe(
            prompt=prompt,
            negative_prompt=neg_prompt,
            image=image,
            num_inference_steps=20,
            guidance_scale=8.5,
            controlnet_conditioning_scale=1.1,
        ).images[0]

    os.makedirs("images", exist_ok=True)
    out_path = os.path.join("images", "transformed_2D.png")
    result.save(out_path)

    return JSONResponse(content={"url": "/images/transformed_2D.png"})


@app.post("/reconstruct_3d")
async def generate_model(request: Request, file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid file type")

    # Read upload into PIL → np array
    data = await file.read()
    img_pil = Image.open(BytesIO(data)).convert("RGB")
    img_np = np.asarray(img_pil)
    H, W, _ = img_np.shape

    # Run MediaPipe FaceMesh
    results = request.app.state.mp_face.process(img_np)
    if not results.multi_face_landmarks:
        raise HTTPException(400, "No face detected")
    lm = results.multi_face_landmarks[0].landmark  # list of 468 landmarks

    # Build and normalize verts
    verts = np.array(
        [[(p.x - 0.5) * 2.0, -(p.y - 0.5) * 2.0, p.z] for p in lm], dtype=np.float32
    )

    # Build triangles for faces
    pts2d = np.array([[p.x, p.y] for p in lm])
    triangles = Delaunay(pts2d)
    faces = triangles.simplices.astype(np.int32)

    # Find and create colors
    colors = []
    for p in lm:
        x = int(min(p.x * W, W - 1))
        y = int(min(p.y * H, H - 1))
        colors.append(img_np[y, x] / 255.0)
    colors = np.array(colors, dtype=np.float32)

    # Building the mesh
    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(verts)
    mesh.triangles = o3d.utility.Vector3iVector(faces)
    mesh.vertex_colors = o3d.utility.Vector3dVector(colors)
    mesh.compute_vertex_normals()

    # Quadric decimation (500k -> 30k triangles)
    # print(
    #     f"Mesh stats before decimation: verts={len(mesh.vertices)}, faces={len(mesh.triangles)}, has_colors={mesh.has_vertex_colors()}"
    # )
    # target_triangles = 100000
    # mesh = mesh.simplify_quadric_decimation(target_number_of_triangles=target_triangles)
    # mesh.compute_vertex_normals()
    # print(
    #     f"Mesh stats after decimation: verts={len(mesh.vertices)}, faces={len(mesh.triangles)}, has_colors={mesh.has_vertex_colors()}"
    # )

    # Save the ply
    img_id = str(uuid.uuid4())
    mesh_name = f"{img_id}.ply"
    mesh_path = os.path.join(tmp_dir, mesh_name)
    o3d.io.write_triangle_mesh(
        mesh_path,
        mesh,
        write_ascii=True,
        write_vertex_colors=True,
        write_vertex_normals=True,
    )
    return JSONResponse(content={"meshUrl": f"/meshes/{mesh_name}"})


@app.on_event("shutdown")
def cleanup():
    for fname in os.listdir(tmp_dir):
        os.remove(os.path.join(tmp_dir, fname))
