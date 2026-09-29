#!/bin/bash

# 定义待检测的 IP 地址
IP1="192.168.1.12"
LIDAR_IP="192.168.1.100"
WAIT_FOR_HOST="/home/amov/p450_recording/bin/wait_for_host.sh"
MID360_WAIT_ATTEMPTS=30
MID360_WAIT_INTERVAL=1

# Ping 第一个 IP 地址
if ping -c 1 $IP1 &> /dev/null; then
    echo "G1: $IP1 is reachable."
    # 在这里运行你想要的命令 G1
    if "$WAIT_FOR_HOST" "$LIDAR_IP" "$MID360_WAIT_ATTEMPTS" "$MID360_WAIT_INTERVAL"; then
        echo "MID360: $LIDAR_IP is reachable."
        # MID360+G1
        gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment p450_onboard_mid360.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 3; roslaunch p450_experiment gimbal_server.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 5; source ~/.bashrc; roslaunch p450_experiment video_streaming.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment switch_location_source_mid360_g1.launch; exit ; exec bash"'
    else
        echo "MID360: $LIDAR_IP is not reachable."
        # S3+G1+t265
        gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment p450_onboard_t265.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 3; roslaunch p450_experiment gimbal_server.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 5; source ~/.bashrc; roslaunch p450_experiment video_streaming.launch; exit ; exec bash"'
    	--tab -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment switch_location_source_mid360_g1.launch; exit ; exec bash"'
    fi
else
    echo "G1: $IP1 is not reachable."
    # 没有G1
    # Ping 第二个 IP 地址
    if "$WAIT_FOR_HOST" "$LIDAR_IP" "$MID360_WAIT_ATTEMPTS" "$MID360_WAIT_INTERVAL"; then
        echo "MID360: $LIDAR_IP is reachable."
        # mid360+d435i
        gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment p450_onboard_mid360.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 3; roslaunch p450_experiment rs_camera_d435i.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 5; source ~/.bashrc; roslaunch p450_experiment video_streaming_d435i.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment switch_location_source_mid360_d435i.launch; exit ; exec bash"'
    else
        echo "MID360: $LIDAR_IP is not reachable."
        # s3+d435i+t265
        gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment p450_onboard_t265.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 3; roslaunch p450_experiment rs_camera_d435i.launch; exit ; exec bash"' \
    	--tab -e 'bash -c "sleep 5; source ~/.bashrc; roslaunch p450_experiment video_streaming_d435i.launch; exit ; exec bash"'
    	--tab -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment switch_location_source_mid360_g1.launch; exit ; exec bash"'
    fi
fi

