from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
from nav2_common.launch import RewrittenYaml
import os


def generate_launch_description():
    go2_navigation_dir = get_package_share_directory('go2_navigation')
    nav2_bringup_dir = get_package_share_directory('nav2_bringup')

    default_params_file = os.path.join(go2_navigation_dir, 'config', 'nav2_params.yaml')
    default_map_file = os.path.join(go2_navigation_dir, 'maps', 'map.yaml')

    use_localization = LaunchConfiguration('use_localization')
    map_file = LaunchConfiguration('map')
    params_file = LaunchConfiguration('params_file')
    use_sim_time = LaunchConfiguration('use_sim_time')
    autostart = LaunchConfiguration('autostart')

    declare_use_localization = DeclareLaunchArgument(
        'use_localization',
        default_value='true',
        description='true: 静态地图+AMCL+导航；false: 外部SLAM提供map->odom，仅启动导航栈'
    )

    declare_map = DeclareLaunchArgument(
        'map',
        default_value=default_map_file,
        description='静态地图 yaml 文件路径'
    )

    declare_params_file = DeclareLaunchArgument(
        'params_file',
        default_value=default_params_file,
        description='Nav2 参数文件路径'
    )

    declare_use_sim_time = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='是否使用仿真时间'
    )

    declare_autostart = DeclareLaunchArgument(
        'autostart',
        default_value='true',
        description='生命周期节点是否自动激活'
    )

    # 关键：显式把 map 文件路径重写到 map_server.yaml_filename
    configured_params = RewrittenYaml(
        source_file=params_file,
        root_key='',
        param_rewrites={
            'use_sim_time': use_sim_time,
            'yaml_filename': map_file,
        },
        convert_types=True
    )

    # ---------- 静态地图定位链 ----------
    map_server_node = Node(
        condition=IfCondition(use_localization),
        package='nav2_map_server',
        executable='map_server',
        name='map_server',
        output='screen',
        parameters=[configured_params]
    )

    amcl_node = Node(
        condition=IfCondition(use_localization),
        package='nav2_amcl',
        executable='amcl',
        name='amcl',
        output='screen',
        parameters=[configured_params]
    )

    lifecycle_manager_localization = Node(
        condition=IfCondition(use_localization),
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager_localization',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': autostart,
            'node_names': ['map_server', 'amcl']
        }]
    )

    # ---------- 导航链 ----------
    navigation_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([
            os.path.join(nav2_bringup_dir, 'launch', 'navigation_launch.py')
        ]),
        launch_arguments={
            'params_file': configured_params,
            'use_sim_time': use_sim_time,
            'autostart': autostart,
        }.items(),
    )

    return LaunchDescription([
        declare_use_localization,
        declare_map,
        declare_params_file,
        declare_use_sim_time,
        declare_autostart,

        map_server_node,
        amcl_node,
        lifecycle_manager_localization,
        navigation_launch,
    ])
