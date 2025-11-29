"use client";

import { useState, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Check, Code, ExternalLink } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/api/agents";
import { updateEmbedSettings } from "@/lib/api/agents";

interface EmbedAgentDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agent: Agent;
}

interface EmbedSettings {
  public_id: string;
  embed_enabled: boolean;
  allowed_domains: string[];
  embed_settings: Record<string, unknown>;
  script_tag: string;
  iframe_code: string;
}

const POSITION_OPTIONS = [
  { value: "bottom-right", label: "Bottom Right" },
  { value: "bottom-left", label: "Bottom Left" },
  { value: "top-right", label: "Top Right" },
  { value: "top-left", label: "Top Left" },
] as const;

const THEME_OPTIONS = [
  { value: "auto", label: "Auto (System)" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
] as const;

export function EmbedAgentDialog({ open, onOpenChange, agent }: EmbedAgentDialogProps) {
  const queryClient = useQueryClient();
  const [copiedScript, setCopiedScript] = useState(false);
  const [copiedIframe, setCopiedIframe] = useState(false);
  const [position, setPosition] = useState("bottom-right");
  const [theme, setTheme] = useState("auto");
  const [buttonText, setButtonText] = useState("Talk to us");
  const [isSaving, setIsSaving] = useState(false);

  // Fetch embed settings
  const { data: embedSettings, isLoading } = useQuery<EmbedSettings>({
    queryKey: ["agent-embed", agent.id],
    queryFn: async () => {
      const response = await api.get(`/api/v1/agents/${agent.id}/embed`);
      return response.data;
    },
    enabled: open,
  });

  // Initialize button text from embed settings
  useEffect(() => {
    if (embedSettings?.embed_settings) {
      const settings = embedSettings.embed_settings as { button_text?: string };
      if (settings.button_text) {
        setButtonText(settings.button_text);
      }
    }
  }, [embedSettings]);

  // Save button text mutation
  const saveButtonText = async (text: string) => {
    setIsSaving(true);
    try {
      await updateEmbedSettings(agent.id, {
        embed_settings: { button_text: text },
      });
      await queryClient.invalidateQueries({ queryKey: ["agent-embed", agent.id] });
      toast.success("Button text saved");
    } catch {
      toast.error("Failed to save button text");
    } finally {
      setIsSaving(false);
    }
  };

  // Reset states when dialog closes
  useEffect(() => {
    if (!open) {
      setCopiedScript(false);
      setCopiedIframe(false);
    }
  }, [open]);

  // Generate embed codes with selected options
  const getScriptTag = () => {
    if (!embedSettings) return "";
    const baseUrl = typeof window !== "undefined" ? window.location.origin : "";
    return `<!-- Voice Noob Widget -->
<script src="${baseUrl}/widget/v1/widget.js" defer></script>
<voice-agent
  agent-id="${embedSettings.public_id}"
  position="${position}"
  theme="${theme}"
></voice-agent>`;
  };

  const getIframeCode = () => {
    if (!embedSettings) return "";
    const baseUrl = typeof window !== "undefined" ? window.location.origin : "";
    return `<iframe
  src="${baseUrl}/embed/${embedSettings.public_id}?position=${position}&theme=${theme}"
  allow="microphone"
  style="position:fixed;${position.includes("bottom") ? "bottom:0;" : "top:0;"}${position.includes("right") ? "right:0;" : "left:0;"}width:300px;height:120px;border:none;z-index:9999;"
></iframe>`;
  };

  const copyToClipboard = async (text: string, type: "script" | "iframe") => {
    try {
      await navigator.clipboard.writeText(text);
      if (type === "script") {
        setCopiedScript(true);
        setTimeout(() => setCopiedScript(false), 2000);
      } else {
        setCopiedIframe(true);
        setTimeout(() => setCopiedIframe(false), 2000);
      }
      toast.success("Copied to clipboard");
    } catch {
      toast.error("Failed to copy");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Code className="h-5 w-5" />
            Embed Voice Agent
          </DialogTitle>
          <DialogDescription>
            Add &quot;{agent.name}&quot; to your website with one of these options
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex h-48 items-center justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        ) : embedSettings ? (
          <div className="space-y-4">
            {/* Widget Options */}
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="position">Position</Label>
                <Select value={position} onValueChange={setPosition}>
                  <SelectTrigger id="position">
                    <SelectValue placeholder="Select position" />
                  </SelectTrigger>
                  <SelectContent>
                    {POSITION_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="theme">Theme</Label>
                <Select value={theme} onValueChange={setTheme}>
                  <SelectTrigger id="theme">
                    <SelectValue placeholder="Select theme" />
                  </SelectTrigger>
                  <SelectContent>
                    {THEME_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Button Text */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="buttonText">Button Text</Label>
                <span className="text-xs text-muted-foreground">{buttonText.length}/20</span>
              </div>
              <div className="flex gap-2">
                <Input
                  id="buttonText"
                  value={buttonText}
                  onChange={(e) => setButtonText(e.target.value.slice(0, 20))}
                  placeholder="Talk to us"
                  maxLength={20}
                  className="flex-1"
                />
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void saveButtonText(buttonText)}
                  disabled={isSaving}
                >
                  {isSaving ? "..." : "Save"}
                </Button>
              </div>
            </div>

            <Tabs defaultValue="script" className="w-full">
              <TabsList className="grid w-full grid-cols-2">
                <TabsTrigger value="script">Script Tag</TabsTrigger>
                <TabsTrigger value="iframe">iframe</TabsTrigger>
              </TabsList>

              <TabsContent value="script" className="space-y-3">
                <div className="relative">
                  <pre className="max-h-40 overflow-auto rounded-lg bg-muted p-4 pr-12 text-sm">
                    <code className="block whitespace-pre-wrap break-all text-muted-foreground">
                      {getScriptTag()}
                    </code>
                  </pre>
                  <Button
                    size="sm"
                    variant="secondary"
                    className="absolute right-2 top-2"
                    onClick={() => void copyToClipboard(getScriptTag(), "script")}
                  >
                    {copiedScript ? (
                      <Check className="h-4 w-4 text-green-500" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                  </Button>
                </div>
                <p className="text-sm text-muted-foreground">
                  Paste this code before the closing{" "}
                  <code className="rounded bg-muted px-1">&lt;/body&gt;</code> tag on your website.
                  The widget will appear as a floating button in the corner.
                </p>
              </TabsContent>

              <TabsContent value="iframe" className="space-y-3">
                <div className="relative">
                  <pre className="max-h-40 overflow-auto rounded-lg bg-muted p-4 pr-12 text-sm">
                    <code className="block whitespace-pre-wrap break-all text-muted-foreground">
                      {getIframeCode()}
                    </code>
                  </pre>
                  <Button
                    size="sm"
                    variant="secondary"
                    className="absolute right-2 top-2"
                    onClick={() => void copyToClipboard(getIframeCode(), "iframe")}
                  >
                    {copiedIframe ? (
                      <Check className="h-4 w-4 text-green-500" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                  </Button>
                </div>
                <p className="text-sm text-muted-foreground">
                  Use an iframe for more control over placement and sizing. Make sure to include{" "}
                  <code className="rounded bg-muted px-1">allow=&quot;microphone&quot;</code> for
                  voice access.
                </p>
              </TabsContent>
            </Tabs>

            {/* Agent ID for reference */}
            <div className="rounded-lg border bg-muted/50 p-3">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Public Agent ID</p>
                  <p className="font-mono text-sm">{embedSettings.public_id}</p>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    void navigator.clipboard.writeText(embedSettings.public_id);
                    toast.success("Agent ID copied");
                  }}
                >
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>

            {/* Preview link */}
            <div className="flex items-center justify-between rounded-lg border p-3">
              <div>
                <p className="text-sm font-medium">Preview Widget</p>
                <p className="text-xs text-muted-foreground">Test the widget before embedding</p>
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  window.open(
                    `/embed/${embedSettings.public_id}/preview?position=${position}&theme=${theme}`,
                    "_blank"
                  );
                }}
              >
                <ExternalLink className="mr-2 h-3.5 w-3.5" />
                Open Preview
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex h-48 items-center justify-center">
            <p className="text-muted-foreground">Failed to load embed settings</p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
