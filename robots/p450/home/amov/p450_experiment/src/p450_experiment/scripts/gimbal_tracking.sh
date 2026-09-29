gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment gimbal_detection_with_tracking.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 2; source ~/.bashrc; roslaunch p450_experiment gimbal_yolov5_tracking_g1.launch; exit ; exec bash"' \

sleep 3
rostopic pub -1 /uav1/prometheus/param_settings prometheus_msgs/ParamSettings "param_name:
- '/video_streaming/mode'
param_value:
- '1'"
