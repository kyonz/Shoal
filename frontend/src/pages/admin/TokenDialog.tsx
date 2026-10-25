import { AlertTriangle } from "lucide-react";
import { Button, CopyField, Dialog } from "@/components/ui";

/** Shows a newly minted secret exactly once. */
export function TokenDialog({
  token,
  onClose,
  title,
  description,
}: {
  token: string | null;
  onClose: () => void;
  title: string;
  description: string;
}) {
  return (
    <Dialog
      open={!!token}
      onOpenChange={(o) => !o && onClose()}
      title={title}
      description={description}
      footer={
        <Button variant="primary" onClick={onClose}>
          I've copied it
        </Button>
      }
    >
      {token && (
        <div className="space-y-3">
          <CopyField value={token} />
          <div className="flex items-start gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-xs text-warn">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            Store this somewhere safe now. Shoal keeps only a hash and cannot show it again.
          </div>
        </div>
      )}
    </Dialog>
  );
}
