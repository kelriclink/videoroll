import { useCallback, useEffect, useMemo, useState } from "react";
import { renderManagementApi, type RenderExecution } from "../../../api/renderManagement";

const ACTIVE_RENDER_STATES = new Set(["claimed", "running", "uploading"]);

export function useTaskRender(taskId: string | undefined) {
  const [renderExecutions, setRenderExecutions] = useState<RenderExecution[]>([]);

  const refreshRenderExecutions = useCallback(async () => {
    if (!taskId) {
      setRenderExecutions([]);
      return;
    }
    try {
      setRenderExecutions(await renderManagementApi.executions({ taskId, limit: 30 }));
    } catch {
      // Render telemetry must not make the task page fail when the management
      // endpoint is momentarily unavailable.
    }
  }, [taskId]);

  useEffect(() => {
    void refreshRenderExecutions();
    if (!taskId) return undefined;
    const timer = window.setInterval(() => void refreshRenderExecutions(), 3000);
    return () => window.clearInterval(timer);
  }, [taskId, refreshRenderExecutions]);

  const activeRenderExecutions = useMemo(
    () => renderExecutions.filter((execution) => ACTIVE_RENDER_STATES.has(execution.state)),
    [renderExecutions],
  );

  return {
    renderExecutions,
    activeRenderExecutions,
    latestRenderExecution: renderExecutions[0] ?? null,
    refreshRenderExecutions,
  };
}
