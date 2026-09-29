#!/bin/bash
catkin_make --source src/Prometheus/Modules/common --build build/common
catkin_make --source src/Prometheus/Modules/uav_control --build build/uav_control
catkin_make --source src/Prometheus/Modules/communication --build build/communication
catkin_make --source src/Prometheus/Modules/tutorial_demo --build build/tutorial_demo
catkin_make --source src/p450_experiment --build build/p450_experiment

# for spirecv-ros
release_num=$(lsb_release -r --short)
echo $release_num
if [ $release_num == "18.04" ]
then
  catkin_make --source src/spirecv-ros/cv_bridge_1804 --build build/cv_bridge
else
  catkin_make --source src/spirecv-ros/cv_bridge_2004 --build build/cv_bridge
fi
catkin_make --source src/spirecv-ros/sv-msgs --build build/msgs
catkin_make --source src/spirecv-ros/sv-srvs --build build/srvs
catkin_make --source src/spirecv-ros/sv-rosapp --build build/rosapp

# for realsense-ros
catkin_make --source src/realsense-ros/realsense2_camera --build build/realsense2_camera
catkin_make --source src/realsense-ros/realsense2_description --build build/realsense2_description

# for mission
catkin_make --source src/mission --build build/mission

# for planning
catkin_make --source src/Prometheus/Modules/ego_planner_swarm --build build/ego_planner_swarm
catkin_make --source src/Prometheus/Modules/motion_planning --build build/motion_planning
catkin_make --source src/Prometheus/Modules/FAST_LIO --build build/FAST_LIO

# for livox_ros_driver2
catkin_make --source src/livox_ros_driver2 --build build/livox_ros_driver2 -DROS_EDITION=ROS1
