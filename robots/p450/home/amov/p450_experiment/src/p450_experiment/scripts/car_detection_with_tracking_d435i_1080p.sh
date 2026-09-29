gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment rs_camera_1080p.launch; exec bash"' \
--tab -e 'bash -c "sleep 3; source ~/.bashrc; roslaunch p450_experiment car_detection_with_tracking_d435i_1080p.launch; exec bash"' \
--tab -e 'bash -c "sleep 5; source ~/.bashrc; roslaunch p450_experiment yolov5_tracking.launch; exec bash"' \
