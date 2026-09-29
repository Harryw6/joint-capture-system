#!/bin/bash

# 定义待检测的 IP 地址
IP1="192.168.1.12"

# Ping 第一个 IP 地址
if ping -c 1 $IP1 &> /dev/null; then
    cd ~/SpireCV/build
    echo "G1: $IP1 is reachable."
    # 在这里运行你想要的命令 G1
    ./CameraCalibrarion -w=5 -h=7 -sl=0.033 -ml=0.021 -d=6 -tp=3 -imw=1280 -imh=720 -fps=30 -ci=0 -c_ip=$IP1 calib.yaml
    mv calib.yaml ~/p450_experiment/src/p450_experiment/config/spirecv_config/calib_g1_720.yaml
    
# D435I
else
    echo "D435I: is reachable."
    YAML_FILE="/home/amov/p450_experiment/src/p450_experiment/config/spirecv_config/calib_webcam_640x480.yaml"
    gnome-terminal -- bash -c "roslaunch p450_experiment rs_camera_480p.launch; exit; exec bash"
    sleep 3
    # 定义话题名称
    TOPIC="/uav1/camera/color/camera_info/K"
    # 初始化变量
    K_DATA=""
    # 循环检测 rostopic 输出是否成功
    while [ -z "$K_DATA" ]; do
        # 获取 rostopic 输出
        K_DATA=$(rostopic echo -n 1 $TOPIC 2>/dev/null | tr -d '[]' | sed '/^---$/d')

        # 如果数据为空，提示重试
        if [ -z "$K_DATA" ]; then
            echo "Failed to retrieve data. Retrying in 1 second..."
            sleep 1  # 每次尝试后等待 1 秒
        fi
    done
    
    # 去掉 "---" 的任何残留部分（确保安全）
    K_DATA=$(echo "$K_DATA" | sed 's/---//g')
    
    # 验证 K_DATA 是否有效
    if [ -n "$K_DATA" ]; then
        echo $K_DATA
        # 替换 YAML 文件中的 data 部分
        sed -i "/camera_matrix:/,/distortion_coefficients:/ s/data: \[.*\]/data: [$K_DATA]/" "$YAML_FILE"
        echo "Updated $YAML_FILE with new camera matrix data."
    else
        echo "Error: Failed to retrieve valid data from $TOPIC. Exiting."
        exit 1
    fi
fi


