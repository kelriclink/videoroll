import { useContext, useEffect, useRef } from "react";

import {
  RealtimeContext,
  type RealtimeEventHandler,
  type RealtimeResyncHandler,
  type RealtimeStatus,
} from "./realtimeContext";

export type { RealtimeEvent } from "./realtimeContext";

export function useRealtimeSubscription(
  topics: string[],
  onEvent: RealtimeEventHandler,
  onResync?: RealtimeResyncHandler,
): RealtimeStatus {
  const context = useContext(RealtimeContext);
  if (!context) throw new Error("useRealtimeSubscription must be used inside RealtimeProvider");
  const { status, subscribe } = context;
  const eventRef = useRef(onEvent);
  const resyncRef = useRef(onResync);
  const key = [...topics].sort().join("|");

  useEffect(() => {
    eventRef.current = onEvent;
    resyncRef.current = onResync;
  }, [onEvent, onResync]);

  useEffect(() => {
    const currentTopics = key ? key.split("|") : [];
    return subscribe(
      currentTopics,
      (event) => eventRef.current(event),
      () => resyncRef.current?.(),
    );
  }, [key, subscribe]);

  return status;
}
