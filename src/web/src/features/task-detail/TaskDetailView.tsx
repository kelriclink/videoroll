import { TaskDetailHeader } from "./components/TaskDetailHeader";
import { TaskDetailLogs } from "./components/TaskDetailLogs";
import { TaskDetailMedia } from "./components/TaskDetailMedia";
import { TaskDetailOverview } from "./components/TaskDetailOverview";
import { TaskDetailPublish } from "./components/TaskDetailPublish";
import { TaskDetailSubtitle } from "./components/TaskDetailSubtitle";
import { TaskDetailDubbing } from "./components/TaskDetailDubbing";
import type { TaskDetailController } from "./useTaskDetailController";

export function TaskDetailView({ controller }: { controller: TaskDetailController }) {
  if (!controller.taskId) return null;

  return (
    <div className="space-y-4">
      <TaskDetailHeader controller={controller} />
      {controller.activeTab === "overview" ? <TaskDetailOverview controller={controller} /> : null}
      {controller.activeTab === "media" ? <TaskDetailMedia controller={controller} /> : null}
      {controller.activeTab === "subtitle" ? (
        <>
          <TaskDetailSubtitle controller={controller} />
          <TaskDetailDubbing
            taskId={controller.taskId}
            task={controller.task}
            assets={controller.assets}
            subtitleJobs={controller.subtitleJobs}
            refreshAssets={controller.refresh}
          />
        </>
      ) : null}
      {controller.activeTab === "logs" ? <TaskDetailLogs controller={controller} /> : null}
      {controller.activeTab === "publish" ? <TaskDetailPublish controller={controller} /> : null}
    </div>
  );
}
