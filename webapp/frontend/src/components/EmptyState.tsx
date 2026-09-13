/** 空状态：一句话 + 原因（口径/环节），不高亮不插画 */
export function EmptyState({ msg, reason }: { msg: string; reason?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 px-3 py-8 text-center">
      <p className="text-[13px] text-muted-foreground">{msg}</p>
      {reason && <p className="text-[11px] text-muted-foreground/70">{reason}</p>}
    </div>
  );
}
