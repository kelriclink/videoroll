import { useParams } from "react-router-dom";
import { TaskDetailView } from "./TaskDetailView";
import { useTaskDetailController } from "./useTaskDetailController";

export default function TaskDetailPage() {
  const { taskId } = useParams();
  const controller = useTaskDetailController(taskId);
  return <TaskDetailView controller={controller} />;
}
