gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment rs_t265.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 3; source ~/.bashrc; roslaunch p450_experiment p450_t265_onboard.launch; exit ; exec bash"' \
