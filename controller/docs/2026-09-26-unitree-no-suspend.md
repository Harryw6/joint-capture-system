# Unitree 禁用自动休眠

2026-09-26，经用户要求，在 unitree@192.168.123.18 配置。

## 检查与边界

- Ubuntu / Jetson，systemd 245；重启后 SSH 可达。
- 无持久化上次启动日志，不能证明此前掉线由休眠造成。
- 原 GNOME power 设置：AC type=suspend、timeout=0；battery type=suspend、timeout=1200。
- 原 sleep/suspend/hibernate/hybrid-sleep targets 为 static，没有本次目标的 /etc mask 覆盖。

## 变更

- unitree 用户的 sleep-inactive-ac-type、sleep-inactive-battery-type 改为 nothing。
- 对应两个 timeout 改为 0。
- systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target。
- 保留关机、电源键、温度保护和采集/遥操配置；没有重启或执行休眠测试。
- 设置持久保存。屏幕熄灭不等于系统休眠，无需为 SSH 保活禁用屏保。

## 如需恢复原设置（用户主动执行）

```bash
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type suspend
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-timeout 0
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type suspend
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-timeout 1200
```

gsettings 应由 unitree 用户执行，不使用 sudo。禁用自动休眠意味着空闲时仍耗电，采集完成后需按正常流程关机。
