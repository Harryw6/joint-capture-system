gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment car_detection_with_tracking_d435i.launch; exit; exec bash"' \
--tab -e 'bash -c "sleep 3; source ~/.bashrc; roslaunch p450_experiment yolov5_tracking.launch; exit; exec bash"' \

sleep 3
rostopic pub -1 /uav1/prometheus/param_settings prometheus_msgs/ParamSettings "param_name:
- '/video_streaming/mode'
param_value:
- '1'"
