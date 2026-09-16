import { SettingsTranslateView } from "./SettingsTranslateView";
import { useSettingsTranslateController } from "./useSettingsTranslateController";

export default function SettingsTranslatePage() {
  const controller = useSettingsTranslateController();
  return <SettingsTranslateView controller={controller} />;
}
