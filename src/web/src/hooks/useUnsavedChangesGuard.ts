import { useEffect, useRef } from "react";
import { useLocation } from "react-router-dom";
import { useConfirm } from "../components/feedbackContext";

export function useUnsavedChangesGuard(
  enabled: boolean,
  {
    title = "有未保存的配置",
    message = "离开当前页面会丢失尚未保存的修改。",
  }: { title?: string; message?: string } = {},
) {
  const confirm = useConfirm();
  const location = useLocation();
  const navigationBypassRef = useRef(false);

  useEffect(() => {
    if (!enabled) return undefined;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return undefined;
    let confirmPending = false;
    const onDocumentClick = (event: MouseEvent) => {
      if (navigationBypassRef.current) {
        navigationBypassRef.current = false;
        return;
      }
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest("a[href]");
      if (!(anchor instanceof HTMLAnchorElement)) return;
      if (anchor.target && anchor.target !== "_self") return;
      if (anchor.hasAttribute("download")) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      const destination = `${url.pathname}${url.search}${url.hash}`;
      const current = `${location.pathname}${location.search}${location.hash}`;
      if (destination === current) return;
      event.preventDefault();
      if (confirmPending) return;
      confirmPending = true;
      void confirm({
        title,
        message,
        confirmLabel: "放弃并离开",
        cancelLabel: "继续编辑",
        tone: "warning",
      }).then((ok) => {
        confirmPending = false;
        if (ok && anchor.isConnected) {
          navigationBypassRef.current = true;
          anchor.click();
        }
      });
    };
    document.addEventListener("click", onDocumentClick, true);
    return () => document.removeEventListener("click", onDocumentClick, true);
  }, [confirm, enabled, location.hash, location.pathname, location.search, message, title]);
}
