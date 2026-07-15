import { createContext, PropsWithChildren, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { orchestratorWebSocketUrl } from "./urls";


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

type RealtimeStatus = "connecting" | "connected" | "reconnecting" | "offline";
type EventHandler = (event: RealtimeEvent) => void;
type ResyncHandler = () => void;
type Subscription = { topics: Set<string>; onEvent: EventHandler; onResync?: ResyncHandler };

type RealtimeContextValue = {
  status: RealtimeStatus;
  subscribe: (topics: string[], onEvent: EventHandler, onResync?: ResyncHandler) => () => void;
};

const RealtimeContext = createContext<RealtimeContextValue | null>(null);

function intersects(left: Set<string>, right: string[]): boolean {
  return right.some((topic) => left.has(topic));
}

export function RealtimeProvider({ children }: PropsWithChildren) {
  const [status, setStatus] = useState<RealtimeStatus>("connecting");
  const socketRef = useRef<WebSocket | null>(null);
  const subscriptionsRef = useRef(new Map<number, Subscription>());
  const nextSubscriptionIdRef = useRef(1);
  const reconnectTimerRef = useRef<number | undefined>();
  const staleTimerRef = useRef<number | undefined>();
  const reconnectAttemptRef = useRef(0);
  const hasConnectedRef = useRef(false);
  const stoppedRef = useRef(false);

  const sendSubscriptions = useCallback(() => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const topics = new Set<string>();
    for (const subscription of subscriptionsRef.current.values()) {
      for (const topic of subscription.topics) topics.add(topic);
    }
    socket.send(JSON.stringify({ op: "set_subscriptions", topics: Array.from(topics).sort() }));
  }, []);

  const notifyResync = useCallback(() => {
    for (const subscription of subscriptionsRef.current.values()) subscription.onResync?.();
  }, []);

  useEffect(() => {
    stoppedRef.current = false;

    const armStaleTimer = () => {
      if (staleTimerRef.current) window.clearTimeout(staleTimerRef.current);
      staleTimerRef.current = window.setTimeout(() => socketRef.current?.close(4000, "heartbeat timeout"), 45_000);
    };

    const connect = () => {
      if (stoppedRef.current) return;
      setStatus(hasConnectedRef.current ? "reconnecting" : "connecting");
      const socket = new WebSocket(orchestratorWebSocketUrl());
      socketRef.current = socket;

      socket.onopen = () => armStaleTimer();
      socket.onmessage = (message) => {
        armStaleTimer();
        let payload: unknown;
        try {
          payload = JSON.parse(String(message.data || ""));
        } catch {
          return;
        }
        if (!payload || typeof payload !== "object") return;
        const record = payload as Record<string, unknown>;
        if (record.type === "ready") {
          const reconnecting = hasConnectedRef.current;
          hasConnectedRef.current = true;
          reconnectAttemptRef.current = 0;
          setStatus("connected");
          sendSubscriptions();
          if (reconnecting) notifyResync();
          return;
        }
        if (record.type === "heartbeat") {
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ op: "pong" }));
          return;
        }
        if (record.type === "resync_required") {
          notifyResync();
          return;
        }
        if (record.type !== "event" || !Array.isArray(record.topics) || typeof record.name !== "string") return;
        const event = record as unknown as RealtimeEvent;
        for (const subscription of subscriptionsRef.current.values()) {
          if (intersects(subscription.topics, event.topics)) subscription.onEvent(event);
        }
      };
      socket.onerror = () => socket.close();
      socket.onclose = (event) => {
        if (socketRef.current === socket) socketRef.current = null;
        if (staleTimerRef.current) window.clearTimeout(staleTimerRef.current);
        if (stoppedRef.current) return;
        if (event.code === 4401) {
          setStatus("offline");
          window.location.reload();
          return;
        }
        if (event.code === 4403) {
          setStatus("offline");
          return;
        }
        setStatus("reconnecting");
        const delays = [1000, 2000, 5000, 10_000, 30_000];
        const delay = delays[Math.min(reconnectAttemptRef.current, delays.length - 1)];
        reconnectAttemptRef.current += 1;
        reconnectTimerRef.current = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      stoppedRef.current = true;
      if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current);
      if (staleTimerRef.current) window.clearTimeout(staleTimerRef.current);
      socketRef.current?.close(1000, "provider unmounted");
      socketRef.current = null;
    };
  }, [notifyResync, sendSubscriptions]);

  const subscribe = useCallback(
    (topics: string[], onEvent: EventHandler, onResync?: ResyncHandler) => {
      const id = nextSubscriptionIdRef.current++;
      subscriptionsRef.current.set(id, { topics: new Set(topics), onEvent, onResync });
      sendSubscriptions();
      return () => {
        subscriptionsRef.current.delete(id);
        sendSubscriptions();
      };
    },
    [sendSubscriptions],
  );

  const value = useMemo(() => ({ status, subscribe }), [status, subscribe]);
  return (
    <RealtimeContext.Provider value={value}>
      {status === "reconnecting" || status === "offline" ? (
        <div className="fixed left-1/2 top-2 z-[100] -translate-x-1/2 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 shadow">
          {status === "offline" ? "实时连接不可用，请检查服务或重新登录" : "实时连接断开，正在重连…"}
        </div>
      ) : null}
      {children}
    </RealtimeContext.Provider>
  );
}

export function useRealtimeSubscription(
  topics: string[],
  onEvent: EventHandler,
  onResync?: ResyncHandler,
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
