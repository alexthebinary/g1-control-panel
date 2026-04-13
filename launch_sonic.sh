#!/bin/bash
cd /home/unitree/GR00T-WholeBodyControl/gear_sonic_deploy
export TensorRT_ROOT=/home/unitree/TensorRT-10.7.0.23
export LD_LIBRARY_PATH=$TensorRT_ROOT/lib:/usr/local/cuda-12.6/lib64:/opt/onnxruntime/lib:$(pwd)/thirdparty/unitree_sdk2/thirdparty/lib/aarch64:$LD_LIBRARY_PATH
export PATH=/usr/local/cuda-12.6/bin:$PATH
./target/release/g1_deploy_onnx_ref enP8p1s0 policy/release/model_decoder.onnx reference/example_full/ --obs-config policy/release/observation_config.yaml --encoder-file policy/release/model_encoder.onnx --planner-file planner/target_vel/V2/planner_sonic.onnx --input-type gamepad_manager --max-close-ratio 1.0
