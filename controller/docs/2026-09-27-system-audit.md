# 联合采集复核（2026-09-27）

结论：当前生产部署仍不能保证完整、稳定的训练数据采集。此轮是只读实机诊断及隔离测试，没有启停采集、使能机械臂、修改飞控或更换生产程序。

## 当前状态

- P450、Unitree 均可 SSH 连接，无活动采集。Unitree `session_prepared=false`，遥操快照已过期，不能从这个状态推断手柄硬件是否配对。
- 两端已重启：P450 boot ID `1664e105-d986-4135-8eb2-94e58022ed22`；Unitree `24319d6d-be68-417a-a73d-f043a22cda10`。本机 8766 控制台端口拒绝连接，服务未运行。
- 可用空间 P450 58 GiB、Unitree 197 GiB；当前不是空间耗尽。
- 使用现有 ClockProbe，每端预热后采 20 个应用层 SSH 往返样本：P450 min/median/max 3.673/4.192/10.890 ms；Unitree 1.323/1.865/9.822 ms。此前另 20 个样本/端也均成功。短时空闲连通不能证明负载下长期稳定，更不是曝光同步证明。

## 已确认问题

### 1. 生产 Unitree 写盘仍丢帧（高优先级）

远端 `config/collection.json` 没有 format_version=2，仍使用 PNG+PKL、4 个保存线程、队列 32、PNG compression=1。`onboard/raw_episode.py` 不存在。隔离验证过的 v2 未发布。

只读重验最新 `joint_20260926_223834_UTCp0800_33dc`：

- summary: frames_enqueued=1370，frames_saved=1039，frames_dropped=331，save_errors=0。
- 图像首尾跨度 48.1397 s，有效帧率 21.5622 FPS，目标 30 FPS。
- 验收器返回失败：低帧率、非零丢帧。原始数据保留；本次未写回报告，桌面 manifest 仍为 complete/postprocess=pending。
- `complete` 只表示停止流程完成，不能等同于完整性或训练可用。

### 2. 旧格式验收可漏过损坏 PNG（本轮复现，高优先级）

`remote/unitree/validate_episode.py:86` 只检查 PNG 签名，没有完整解码/块完整性验证。现有 fixture 的图像内容仅为 PNG 签名加 `fixture`；在临时目录调用完整 validate，返回 ok=true，而 Pillow 无法解码该内容。

这是验收漏洞的复现，不代表已经发现最新采集中存在损坏 PNG。后处理必须增加真正的图像完整性检查；不应把重解码工作加到采集热路径。

### 3. 旧写盘停止缺少图像持久化保证（代码确认，高优先级）

生产 v1 EpisodeWriter 对 PKL 只 flush/close/rename，没有对图像文件及目录执行 fsync。Stop 会等待队列排空，但已关闭的文件仍可能处于系统页缓存，不能承诺紧接断电后全部保留。新版 v2 已包含持久化计数和目录同步，但生产未切换。

### 4. 遥操断连保护未部署（高优先级）

除了已发现的末端逆解跳变，生产 piper_gamepad_teleop.py 仍缺少手柄断开时停止发送新目标的守卫。本地守卫存在并有测试，但远端 SHA256 仍为 `48bd58cf3c0e52f434caaf01aa3aab6c718ce97607acb127c47a87272e894ce4`。断连停止发新命令也不等同于硬件急停；重新连接后的目标一致性还需验证。

### 5. 时钟精度不能代表三路图像同时曝光（需求边界）

最新旧格式数据中的前置/腕部相机最大配对时间差为 42.062 ms，配置允许 50 ms。联合控制器的 10 ms 门槛约束时钟映射估计，不约束相机曝光差。主机接收时间、相机帧配对和状态插值须分别验收；不能以“对时通过”推断三路画面在 10 ms 内同步。

### 6. P450 就绪字段可保留重启前状态（本轮实机确认）

当前 status 四个组件均 running=false，但 stack_phase=ready，owned_processes 的 boot ID 仍为上一启动 `2995a73d-304a-41f5-8bf7-efafc97d8ae7`。该字段来自持久化状态文件，未按当前组件重新计算。桌面目前使用组件状态，不足以证明会误启动，但状态接口自身存在歧义，需要修正。

## 未排除的设备问题

上一轮已读到 Unitree EXT4 clean with errors、error count=82，并发生过约 49 MB SCP 后整机失联。此轮 sudo -n 不允许 tune2fs，因此没有重新读取 superblock，不能声称错误已修复。新 boot ID 只说明又启动过，不证明异常重启。仍需备份、离线文件系统检查，以及负载下供电/网口观察。

## 验证与限制

- 本地采集控制/原始格式/网页回归：361 passed，28.26 s。
- P450 远端在正确 ROS 环境下运行 unittest discover：75 tests OK，3.417 s。
- Windows 全量 P450 pytest 因无 rospy 无法收集；首次远端测试命令覆盖 ROS PYTHONPATH 导致 rosbag 导入失败，修正测试环境后上述 75 项通过。没有安装依赖或修改远端代码。
- 本轮未做新一条实机录制，也未触发大文件传输。历史短段/隔离长段证据见 2026-09-26-unitree-raw-capture-verification.md。
- 测试通过只覆盖已有断言；本轮 PNG 复现说明已有绿灯不能替代数据语义审查。

## 建议处理顺序

先修遥操连续性和断连行为；随后处理 Unitree 磁盘/传输稳定性，发布并实机复验 v2 原始写盘；同时补齐离线 PNG 验收及 P450 当前就绪状态。最后做连续多段、代表性长段和完整离线恢复验收。原始数据、时钟样本和联合 manifest 必须一起归档。
