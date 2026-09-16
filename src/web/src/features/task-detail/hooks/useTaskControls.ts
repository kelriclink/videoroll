import { useConfirm, useToast } from "../../../components/feedbackContext";
import type { Dispatch, SetStateAction } from "react";
import { tasksApi } from "../../../api/tasks";
import type { Task } from "../../../lib/types";

type UseTaskControlsArgs = {
  taskId: string | undefined;
  setTask: Dispatch<SetStateAction<Task | null>>;
  refresh: (opts?: { silent?: boolean }) => Promise<void>;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusy: Dispatch<SetStateAction<boolean>>;
};

export function useTaskControls({ taskId, setTask, refresh, setError, setBusy }: UseTaskControlsArgs) {
  const confirm = useConfirm();
  const toast = useToast();

  async function stopTask() {
    if (!taskId) return;
    const ok = await confirm({
      title: "停止任务",
      message: "停止后不会再启动后续字幕或渲染步骤；正在执行的工作会在当前安全检查点暂停。",
      confirmLabel: "停止",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await tasksApi.stop(taskId);
      setTask(updated);
      await refresh({ silent: true });
      toast({ kind: "success", title: "任务已停止" });
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function resumeStoppedTask() {
    if (!taskId) return;
    const ok = await confirm({
      title: "恢复任务",
      message: "将从停止前的阶段继续，已保存的字幕和渲染产物会被复用。",
      confirmLabel: "恢复",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await tasksApi.resume(taskId);
      setTask(updated);
      await refresh({ silent: true });
      toast({ kind: "success", title: "任务已恢复" });
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return { stopTask, resumeStoppedTask };
}
