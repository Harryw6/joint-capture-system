gnome-terminal --window -e 'bash -c "sleep 1; roslaunch p450_experiment msg_MID360.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 3; roslaunch p450_experiment mapping_mid360.launch; exit ; exec bash"' \
