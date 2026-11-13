import { PageHeader } from "../components/ui";
import BilibiliDiscoveryView from "../features/bilibili-discovery/BilibiliDiscoveryView";

export default function BilibiliDiscoveryPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        title="B站热点与活动"
        description="浏览 B站实时公开排行榜和当前有效期内的活动。活动详情以B站官方页面为准。"
      />
      <BilibiliDiscoveryView />
    </div>
  );
}
