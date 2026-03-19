from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os


def generate_launch_description():
    ld = LaunchDescription()

    go2_core_dir = get_package_share_directory('go2_core')
    go2_navigation_dir = get_package_share_directory('go2_navigation')
    go2_perception_dir = get_package_share_directory('go2_perception')

    rviz_config_path = os.path.join(go2_core_dir, 'config', 'Nav2_default.rviz')
    nav2_params_path = os.path.join(go2_navigation_dir, 'config', 'nav2_params.yaml')
    map_yaml_path = os.path.join(go2_navigation_dir, 'maps', 'map.yaml')

    # 1. 启动底层基础节点
    go2_base_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([
            os.path.join(go2_core_dir, 'launch', 'go2_base.launch.py')
        ]),
        launch_arguments={
            'video_enable': 'true',
            'image_topic': '/camera/image_raw',
            'tcp_enable': 'true',
            'tcp_host': '127.0.0.1',
            'tcp_port': '5432',
            'target_fps': '30',
        }.items()
    )

    # 2. 启动点云处理
    pointcloud_process_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([
            os.path.join(go2_perception_dir, 'launch', 'go2_pointcloud_process.launch.py')
        ])
    )

    # 3. 启动静态地图导航（AMCL + map_server + Nav2）
    nav2_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([
            os.path.join(go2_navigation_dir, 'launch', 'go2_nav2.launch.py')
        ]),
        launch_arguments={
            'use_localization': 'true',
            'map': map_yaml_path,
            'params_file': nav2_params_path,
            'use_sim_time': 'false',
            'autostart': 'true',
        }.items()
    )

    # 4. RViz
    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        arguments=['-d', rviz_config_path],
        output='screen'
    )

    ld.add_action(go2_base_launch)
    ld.add_action(pointcloud_process_launch)
    ld.add_action(nav2_launch)
    ld.add_action(rviz_node)

    return ld