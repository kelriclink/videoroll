import type { ModelProvider } from "../../../api/translate";
import { providerKindLabel } from "./modelProviderForm";

export function ModelProviderSidebar({
  providers,
  selectedId,
  isNew,
  onSelect,
}: {
  providers: ModelProvider[];
  selectedId: string;
  isNew: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="space-y-1">
      {providers.map((provider) => (
        <button
          key={provider.id}
          type="button"
          onClick={() => onSelect(provider.id)}
          className={[
            "flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left text-sm",
            selectedId === provider.id
              ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
              : "text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900",
          ].join(" ")}
        >
          <span className="min-w-0 truncate">{provider.name}</span>
          <span className={[
            "shrink-0 rounded px-1.5 py-0.5 text-[10px]",
            selectedId === provider.id
              ? "bg-white/15 text-current dark:bg-slate-900/10"
              : "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
          ].join(" ")}>
            {providerKindLabel(provider)}
          </span>
        </button>
      ))}
      {isNew ? (
        <div className="rounded-lg bg-sky-50 px-3 py-2.5 text-sm text-sky-800 dark:bg-sky-950/30 dark:text-sky-300">
          新模型服务
        </div>
      ) : null}
    </div>
  );
}
