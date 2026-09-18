export function knowledgeItemHref(itemId: string): string {
  return `/knowledge?${new URLSearchParams({ item: itemId }).toString()}`;
}
