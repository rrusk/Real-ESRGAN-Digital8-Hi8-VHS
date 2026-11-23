> Retained upstream Real-ESRGAN FAQ. Its image-inference examples describe the
> upstream interfaces. For this fork's video pipeline, start with the
> [project README](../README.md) and [installation guide](../INSTALL.md).

# FAQ

1. **Q: How to select models?**<br>
A: Please refer to [docs/model_zoo.md](model_zoo.md)

1. **Q: Can `face_enhance` be used for anime images/animation videos?**<br>
A: No, it can only be used for real faces. It is recommended not to use this option for anime images/animation videos to save GPU memory.

1. **Q: Error "slow_conv2d_cpu" not implemented for 'Half'**<br>
A: In order to save GPU memory consumption and speed up inference, Real-ESRGAN uses half precision (fp16) during inference by default. However, some operators for half inference are not implemented in CPU mode. You need to add **`--fp32` option** for the commands. For example, `python inference_realesrgan.py -n RealESRGAN_x4plus.pth -i inputs --fp32`.
