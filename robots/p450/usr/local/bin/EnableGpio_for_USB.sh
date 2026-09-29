#!/bin/bash

sleep 10

echo 433 > /sys/class/gpio/export
echo out > /sys/class/gpio/PN.01/direction
echo 1 > /sys/class/gpio/PN.01/value
sleep 5
echo 0 > /sys/class/gpio/PN.01/value

