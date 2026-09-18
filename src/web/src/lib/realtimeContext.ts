import { createContext } from "react";

export type RealtimeEvent = {
  v?: number;
  type: "event";
  event_id: string;
  topics: string[];
  name: string;
  occurred_at?: string;
  entity_id?: string | null;
  data: Record<string, unknown>;
};

export type RealtimeStatus = "connecting" | "connected" | "reconnecting" | "offline";
export type RealtimeEventHandler = (event: RealtimeEvent) => void;
export type RealtimeResyncHandler = () => void;
export type RealtimeSubscription = {
  topics: Set<string>;
  onEvent: RealtimeEventHandler;
  onResync?: RealtimeResyncHandler;
};

export type RealtimeContextValue = {
  status: RealtimeStatus;
  subscribe: (
    topics: string[],
    onEvent: RealtimeEventHandler,
    onResync?: RealtimeResyncHandler,
  ) => () => void;
};

export const RealtimeContext = createContext<RealtimeContextValue | null>(null);
