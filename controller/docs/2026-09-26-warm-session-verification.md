# 2026-09-26 常驻设备与快速分段验证

## 已部署

- Unitree 相机由 camera_session.py 常驻持有；每段单独启动／停止记录线程。
- 手柄进程常驻，切换每段 CSV 时先关闭并刷出上一段，再确认停止。保留原 Home 使能行为。
- Go2 记录桥与 Piper 文件记录线程每段结束后退出。SDK 的单 CAN 接收器在相机服务内复用。
- 停止失败保留活动标记，允许重试；检查 PID 的启动时间与命令参数，不误杀其他进程。
- Stop 不做视频导出、完整数据验收，也不停止机器人运动；只停止记录并保存结束信息。
- 看板显示前置／腕部相机实时状态；相机异常禁止直接开始，但允许重新初始化。

备份：

- Unitree：/home/unitree/heterovla-collection/backups/20260926-warm-session/
- Windows：D:/OneDriveData/Desktop/JointCaptureBackup-20260926-warm-session/

## 实机证据

修复接线前，腕部相机曾无序列号以 USB2 枚举，随后 USB3 枚举后再次断开。初始化明确失败，并清理相机服务；连续两次 Stop 均返回无活动采集。未删除任何原始数据。

重新插拔后，两路相机均恢复 USB3 5000 Mbps，序列号 front=254843065497、wrist=254843064363。两次联合采集期间读取失败均为 0。

| 测试 | 会话 | Unitree 保存帧 | 写盘队列丢帧 | UI Stop 耗时 |
|---|---|---:|---:|---:|
| 1 | joint_20260926_040729_5e0f | 1495 | 100 | 6.72 s |
| 2 | joint_20260926_040905_0894 | 1954 | 153 | 6.91 s |

第二段没有显式初始化。相机服务 PID=48261、手柄 PID=48310 在两段之间保持不变。Unitree 启动命令耗时分别约 1.89、2.09 秒（不包含联合就绪／时钟验收）。两段结束后相机服务线程数恢复至 9，手柄 12；不再有分段 bridge PID 文件和 active_episode。

原始数据：

- Unitree：/home/unitree/heterovla-data/datasets/diagnostic_20260926_warm_01/joint_20260926_040729_5e0f
- Unitree：/home/unitree/heterovla-data/datasets/diagnostic_20260926_warm_02/joint_20260926_040905_0894
- P450：/home/amov/p450_data/19700101_084359_joint_20260926_040729_5e0f
- P450：/home/amov/p450_data/19700101_084535_joint_20260926_040905_0894
- 本机：D:/OneDriveData/Desktop/joint_manifests/ 下同名会话；保留双端 clock_*.jsonl 与 manifest.json。

抽样解码了两幅 640×480 RGB 图像；PKL 同时包含 Go2、Piper 状态及诊断时间。P450 两条完整 .bag 分别约 20.9 MB、28.8 MB。没有导出视频。

## 未通过／待处理

1. 录制与停止成功不等于完整验收通过。静止测试未按 Home，机械臂未使能；不能据此声称遥操运动验收通过。
2. 当前 PNG level=1 在此场景出现写入队列溢出。抽样单线程两幅图编码约 114 ms、820 KB；level=0 约 56 ms、1847 KB。两者都无损；尚未修改配置，等待用户对空间／速度取舍的选择。
3. 启动时软件对时估计约 2.8–2.9 ms。第一条保存的时钟探测可重建两端映射；第二条 Unitree 后验模型拒绝 sequence=233：残差 2470842 ns，允许区间 1428696 ns。该段本机 wall/monotonic 配对存在数毫秒波动，需要进一步检查采样抖动。没有放宽阈值或忽略异常，完整对时验收仍待处理。

## 自动验证

- 257 个本地 tests 测试通过。
- Unitree Python 3.8 编译检查、bash 语法检查通过。
- 浏览器实测 1366×768：状态在上、开始按钮首屏可见；结束后 Start 可点击、Stop 禁用。
- 最终状态：两端空闲；P450 定位有效／未解锁；双相机持续出帧；手柄连接，机械臂未使能。
