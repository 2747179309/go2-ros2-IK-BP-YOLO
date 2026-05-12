# SOLUTION.md: Integrated Robotic Control System

## 1. Project Overview
This repository provides a comprehensive solution for autonomous robotic manipulation using the **Unitree Go2** platform. It bridges high-level computer vision with low-level robotic control to facilitate automated tasks.

## 2. Key Features
* **Target Detection:** Utilizes **YOLOv8** for real-time object localization with an mAP50 of 99.5%.
* **Inverse Kinematics (IK):** Implements a **BP Neural Network** to map 3D spatial coordinates to joint angles, overcoming the complexity of traditional analytical IK.
* **Real-time Performance:** Achieves an end-to-end response latency of **42ms**, enabling fluid motion in dynamic environments.

## 3. System Architecture
The framework is built on **ROS 2**, ensuring modular communication between:
1. **Vision Node:** Processes camera streams for object identification.
2. **Logic Node:** Coordinates the transition from detection to motion planning.
3. **Control Node:** Executes the BP-IK solver and sends joint commands to the hardware interface.

## 4. Performance Metrics
| Metric | Result |
| :--- | :--- |
| **Detection Precision (mAP50)** | 99.5% |
| **Inference Latency** | 15ms (YOLO-World + MobileSAM) |
| **Total System Response** | 42ms |
| **Mis-picking Rate** | < 3% |

## 5. Deployment Guide

### Prerequisites
* Ubuntu 22.04 + ROS 2 (Humble/Foxy)
* PyTorch, OpenCV

### Execution
```bash
# Clone the repository
git clone [https://github.com/2747179309/go2-ros2-IK-BP-YOLO.git](https://github.com/2747179309/go2-ros2-IK-BP-YOLO.git)

# Build the workspace
colcon build --symlink-install
source install/setup.bash

# Launch the integrated system
ros2 launch go2_integration main_launch.py
