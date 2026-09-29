# Unitree v2 显式试运行部署

用户明确要求直接部署已实现的新流程试跑。本次切换是带已知存储风险的诊断试运行，不代表系统盘维护门槛已通过。

## 部署

- 使用 `scripts/activate_unitree_raw_trial.py`，在设备操作锁内确认没有 active episode 和待关闭相机会话。
- 备份 `/home/unitree/heterovla-collection/backups/raw-trial-20260927`。
- MCAP 1.3.0 从此前验收的隔离目录复制到 onboard 私有目录，不改系统 Python 包。
- 配置启用 `format_version=2`、`raw_codec=lz4`、每路 64 MiB 有界队列。原始 BGR8 独立保存，PNG/视频编码和图像配对留在离线阶段。
- 只重启相机 owner，更新后 PID 29836；遥操 PID 22071 全程保持，没有发送 Home、使能、回零或运动指令。现场机械臂已使能，测试不代表实际动作安全验收。

## 本次实机结果

经当前已安装控制台连续 Start/Stop，两条之间未初始化：

| episode | 每路帧数 | 前置/腕部 FPS | Unitree Stop SSH 时长 | 共同区间 |
| --- | --- | --- | --- | --- |
| joint_20260927_101623_UTCp0800_28ce | 1229 / 1229 | 29.976 / 29.980 | 1.445 s | 15.880 s |
| joint_20260927_101829_UTCp0800_1a1a | 1625 / 1625 | 29.980 / 29.982 | 1.113 s | 29.649 s |

图像保存包含共同开始前的预录，实际图像跨度约 40.97 s 和 54.17 s，不应把全部帧数除以共同区间计算 FPS。两次均 received=accepted=written=durable，rejected/write_errors=0，state_gap_ticks=0。两端停止成功，时钟监控正常关闭；期间观察的时钟相对不确定度约 2.3–3.3 ms，不替代最终离线对时验收。

分别在远端运行 `validate_episode.py --episode ... --config ...` 全量读取校验，两条均 `ok=true`、`application_integrity=true`、`raw_closed=true`、`errors=[]`。保留 `training_ready=false` 与 `alignment_valid=null`；wireless_controller.csv 为空是既有允许警告，USB 手柄 piper_gamepad.csv 均有连续记录。不声称传感器硬件层绝对无丢帧，没有自动导出视频。

数据在 `/home/unitree/heterovla-data/datasets/<episode>/<episode>/raw/cameras`；原始状态/元数据同目录保留。P450 对应目录和桌面 `D:/OneDriveData/Desktop/joint_manifests/<episode>` 不变。

本地重新运行 tests：370 passed in 24.69 s（使用已有隔离依赖与正确 PYTHONPATH）。最初裸 pytest 出现 8 项收集错误：Windows 无 rospy、未载入隔离 mcap 和项目路径；纠正环境后 tests 全部通过。P450 ROS 测试此次未重跑，未修改 P450 软件。

## 未消除的风险

- Unitree 文件系统仍为 clean with errors，测试后计数仍 82、无新增已观察 EXT4/NVMe I/O 错误；这不是离线修复或硬件健康证明。
- 以前大文件网络传输失联未复现/排除，本次只远端校验，没有大文件下载。
- 本次短段通过不能代替长时间、动态运动负载验收。
- 空闲状态接口 format_version 默认返回 1；实际配置/两个 episode meta 为 2，录制中接口也正确识别为 2。此空闲显示问题未在本次更改。

最终无活动采集，相机与遥操保持常驻。v2 配置保留供用户后续试运行；旧数据未改写或删除。
