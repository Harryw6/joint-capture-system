# 用于驱动T265-USB3.0接口，相当于插拔一次

export SUDO_PASS="amov"
echo $SUDO_PASS | sudo -S /usr/local/bin/EnableGpio_for_USB.sh \

gnome-terminal --window -e 'bash -c "roslaunch p450_experiment rs_t265.launch; exit ; exec bash"' \
