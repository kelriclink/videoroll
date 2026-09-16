export type {
  CurrentMedia,
  LiveAudioControlAction,
  LiveAudioPlaylist,
  LiveDashboard,
  LiveInputSource,
  LiveMedia,
  LivePlaylist,
  LiveSession,
  LiveSettings,
  PlaybackMode,
  PlaylistItem,
  TaskVideo,
} from "../../api/live";

export type ResourceTab = "video" | "audio" | "source" | "playlist";
export type MediaCandidate = {
  item: import("../../api/live").PlaylistItem;
  displayName: string;
  subtitle: string;
  sizeBytes?: number | null;
  sizeLabel?: string;
};
