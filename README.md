# Unitree Go2 ROS 2 Workspace

## Introduction
This repository hosts the code and resources for integrating the Unitree Go2 robot with ROS 2. The Unitree Go2 brings advanced robotics technology to your fingertips, and with this workspace, you can leverage its full potential within the ROS 2 ecosystem.

## Requirements
- **ROS 2 Foxy Fitzroy** or higher
- **Ubuntu 20.04** (recommended)
- Unitree Go2 robot

## Installation

1. Install Official Unitree ROS2 Package
First, install the official Unitree ROS2 package:

# Follow the official installation guide
# https://github.com/unitreerobotics/unitree_ros2
2. Install Dependencies
ROS2 Packages
sudo apt-get install ros-foxy-navigation2 \
                     ros-foxy-nav2-bringup \
                     ros-foxy-pcl-ros \
                     ros-foxy-tf-transformations \
                     ros-foxy-slam-toolbox
3. Build the Workspace
# Create workspace
mkdir -p go2_ros2_ws/src
cd go2_ros2_ws/src

# Clone repository
git clone https://github.com/andy-zhuo-02/go2_ros2_toolbox.git

# Build
cd ..
colcon build
🎯 Usage
Quick Start
# Source the workspace
source install/setup.bash

# Launch the robot
ros2 launch go2_core go2_startup.launch.py
SLAM Operations
Map Serialization: Save generated maps for later use
Map Deserialization: Load previously saved maps
Navigation
Open RViz2
Select the 'Navigation2 Goal' button
Click on the map to set navigation goals
Drag to adjust the target orientation

## Features
- Real-time control of the Unitree Go2
- Sensor data streaming and processing
- Easy navigation and mapping integration

## Contributing
We welcome contributions! Please fork the repository, make your changes, and submit a pull request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgements
- Special thanks to Unitree for providing the Go2 robot.
- Thanks to the ROS community for their ongoing support.

## Contact
For inquiries, contact [Haonan](mailto:2747179309@qq.com).
