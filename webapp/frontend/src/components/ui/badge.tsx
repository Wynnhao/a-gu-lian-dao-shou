import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * 终端小方标：方角、小号、低饱和。
 * executed/approved/proposed/rejected/report_only 等状态语义见 format.ts。
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 whitespace-nowrap border px-1.5 py-0 text-[11px] font-medium leading-[18px] [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        outline: "border-border bg-transparent text-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        success: "border-transparent bg-down/10 text-down",
        navy: "border-transparent bg-primary/10 text-primary",
        warn: "border-transparent bg-warn/10 text-warn",
        destructive: "border-transparent bg-up/10 text-up",
        neutral: "border-transparent bg-muted text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
