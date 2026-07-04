"""orch —— 异构多智能体编排系统（实现 orchestrator-spec.md）。

子包按 spec §14 模块划分：
  protocol/  信封协议与 schema 校验（§3、附录A）
  store/     事件日志 / 派发表 / 黑板等持久化（§4）
  scheduler/ 调度核心环与崩溃恢复（§5、§9）
  adapters/  执行后端适配层（§7）
  render/    视图组装（§6，M1）
  verify/    验证钩子（§8.3，M2）
  cli/       用户界面（§12，M2）
"""
