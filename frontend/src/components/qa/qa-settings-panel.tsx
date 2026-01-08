"use client";

import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Slider } from "@/components/ui/slider";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Shield,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Loader2,
  RotateCcw,
  Save,
  Info,
} from "lucide-react";
import { InfoTooltip } from "@/components/ui/info-tooltip";
import {
  getWorkspaceQASettings,
  updateWorkspaceQASettings,
  getQAStatus,
  type WorkspaceQASettingsUpdate,
} from "@/lib/api/qa";

// Available evaluation models
const EVALUATION_MODELS = [
  { value: "claude-sonnet-4-20250514", label: "Claude Sonnet 4 (Recommended)" },
  { value: "claude-3-5-sonnet-20241022", label: "Claude 3.5 Sonnet" },
  { value: "claude-3-haiku-20240307", label: "Claude 3 Haiku (Faster)" },
];

interface QASettingsPanelProps {
  workspaceId: string;
}

export function QASettingsPanel({ workspaceId }: QASettingsPanelProps) {
  const queryClient = useQueryClient();

  // Local form state
  const [formState, setFormState] = useState<WorkspaceQASettingsUpdate>({});
  const [hasChanges, setHasChanges] = useState(false);

  // Fetch global QA status
  const { data: qaStatus, isLoading: statusLoading } = useQuery({
    queryKey: ["qa-status"],
    queryFn: getQAStatus,
  });

  // Fetch workspace QA settings
  const {
    data: settingsData,
    isLoading: settingsLoading,
    error: settingsError,
  } = useQuery({
    queryKey: ["qa-workspace-settings", workspaceId],
    queryFn: () => getWorkspaceQASettings(workspaceId),
    enabled: !!workspaceId,
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: (updates: WorkspaceQASettingsUpdate) =>
      updateWorkspaceQASettings(workspaceId, updates),
    onSuccess: () => {
      toast.success("QA settings updated successfully");
      void queryClient.invalidateQueries({ queryKey: ["qa-workspace-settings", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["qa-status"] });
      setHasChanges(false);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to update QA settings");
    },
  });

  // Initialize form state when data loads
  useEffect(() => {
    if (settingsData?.settings) {
      setFormState({
        qa_enabled: settingsData.settings.qa_enabled,
        auto_evaluate: settingsData.settings.auto_evaluate,
        pass_threshold: settingsData.settings.pass_threshold,
        evaluation_model: settingsData.settings.evaluation_model,
        inherit_global: settingsData.settings.inherit_global,
      });
      setHasChanges(false);
    }
  }, [settingsData]);

  // Handle form field changes
  const handleChange = <K extends keyof WorkspaceQASettingsUpdate>(
    field: K,
    value: WorkspaceQASettingsUpdate[K]
  ) => {
    setFormState((prev) => ({ ...prev, [field]: value }));
    setHasChanges(true);
  };

  // Reset to saved values
  const handleReset = () => {
    if (settingsData?.settings) {
      setFormState({
        qa_enabled: settingsData.settings.qa_enabled,
        auto_evaluate: settingsData.settings.auto_evaluate,
        pass_threshold: settingsData.settings.pass_threshold,
        evaluation_model: settingsData.settings.evaluation_model,
        inherit_global: settingsData.settings.inherit_global,
      });
      setHasChanges(false);
    }
  };

  // Save changes
  const handleSave = () => {
    updateMutation.mutate(formState);
  };

  const isLoading = statusLoading || settingsLoading;
  const effectiveSettings = settingsData?.effective_settings;
  const isInheriting = formState.inherit_global ?? settingsData?.settings?.inherit_global ?? true;

  if (settingsError) {
    const errorMessage = settingsError instanceof Error ? settingsError.message : "Unknown error";
    return (
      <Card className="border-destructive/50">
        <CardContent className="flex flex-col items-center justify-center py-8">
          <AlertTriangle className="mb-2 h-8 w-8 text-destructive" />
          <p className="text-sm text-muted-foreground">Failed to load QA settings</p>
          <p className="text-xs text-muted-foreground">{errorMessage}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      {/* Status Card */}
      <StatusCard qaStatus={qaStatus} isLoading={isLoading} />

      {/* Workspace Override Section */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base">Workspace Override</CardTitle>
            <InfoTooltip 
              content="Override global QA settings for this specific workspace. Useful when different projects need different evaluation criteria or thresholds."
              side="right"
            />
          </div>
          <CardDescription>
            Choose whether to use global settings or customize for this workspace
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-10 w-full" />
          ) : (
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <Label htmlFor="inherit-global" className="text-sm font-medium">
                    Use workspace-specific settings
                  </Label>
                  <InfoTooltip 
                    content="When enabled, this workspace will use its own QA settings instead of inheriting from global defaults. This allows you to customize evaluation thresholds and models per project."
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  {isInheriting
                    ? "Currently inheriting from global settings"
                    : "Using custom workspace settings"}
                </p>
              </div>
              <Switch
                id="inherit-global"
                checked={!isInheriting}
                onCheckedChange={(checked) => handleChange("inherit_global", !checked)}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {/* Evaluation Settings */}
      <Card className={isInheriting ? "opacity-60" : ""}>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">Evaluation Settings</CardTitle>
              <CardDescription>Configure how calls are evaluated</CardDescription>
            </div>
            {isInheriting && (
              <Badge variant="secondary" className="text-xs">
                <Info className="mr-1 h-3 w-3" />
                Using Global
              </Badge>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {isLoading ? (
            <div className="space-y-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : (
            <>
              {/* QA Enabled Toggle */}
              <SettingRow
                label="QA Enabled"
                description="Enable quality evaluation for calls in this workspace"
                disabled={isInheriting}
                tooltip="When enabled, completed calls will be automatically analyzed for quality metrics like intent completion, compliance, and response quality."
              >
                <Switch
                  checked={
                    isInheriting
                      ? effectiveSettings?.qa_enabled
                      : (formState.qa_enabled ?? true)
                  }
                  onCheckedChange={(checked) => handleChange("qa_enabled", checked)}
                  disabled={isInheriting}
                />
              </SettingRow>

              {/* Auto-Evaluate Toggle */}
              <SettingRow
                label="Auto-Evaluate Calls"
                description="Automatically evaluate calls when they complete"
                disabled={isInheriting}
                tooltip="When enabled, every completed call is automatically sent for AI evaluation. Disable this if you prefer to manually trigger evaluations for specific calls."
              >
                <Switch
                  checked={
                    isInheriting
                      ? effectiveSettings?.auto_evaluate
                      : (formState.auto_evaluate ?? true)
                  }
                  onCheckedChange={(checked) => handleChange("auto_evaluate", checked)}
                  disabled={isInheriting}
                />
              </SettingRow>

              {/* Pass Threshold Slider */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <Label className={isInheriting ? "text-muted-foreground" : ""}>
                        Pass Threshold
                      </Label>
                      <InfoTooltip 
                        content="The minimum overall score (0-100) a call must achieve to be marked as 'passed'. Lower values are more lenient, higher values are stricter. A score of 70 is recommended for most use cases."
                      />
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Minimum score to pass evaluation (0-100)
                    </p>
                  </div>
                  <Badge variant="outline" className="font-mono">
                    {isInheriting
                      ? effectiveSettings?.pass_threshold
                      : (formState.pass_threshold ?? 70)}
                  </Badge>
                </div>
                <Slider
                  value={[
                    isInheriting
                      ? (effectiveSettings?.pass_threshold ?? 70)
                      : (formState.pass_threshold ?? 70),
                  ]}
                  onValueChange={([value]) => handleChange("pass_threshold", value)}
                  min={0}
                  max={100}
                  step={5}
                  disabled={isInheriting}
                  className={isInheriting ? "opacity-50" : ""}
                />
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>0 (Lenient)</span>
                  <span>100 (Strict)</span>
                </div>
              </div>

              {/* Evaluation Model Dropdown */}
              <SettingRow
                label="Evaluation Model"
                description="AI model used for call evaluation"
                disabled={isInheriting}
                tooltip="Choose the AI model that analyzes your calls. Claude Sonnet 4 offers the best accuracy, while Claude 3 Haiku is faster but less detailed. The model affects both evaluation quality and cost."
              >
                <Select
                  value={
                    isInheriting
                      ? effectiveSettings?.evaluation_model
                      : (formState.evaluation_model ?? "claude-sonnet-4-20250514")
                  }
                  onValueChange={(value) => handleChange("evaluation_model", value)}
                  disabled={isInheriting}
                >
                  <SelectTrigger className="w-[220px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {EVALUATION_MODELS.map((model) => (
                      <SelectItem key={model.value} value={model.value}>
                        {model.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </SettingRow>
            </>
          )}
        </CardContent>
      </Card>

      {/* Action Buttons */}
      <div className="flex justify-end gap-2">
        <Button
          variant="outline"
          onClick={handleReset}
          disabled={!hasChanges || updateMutation.isPending}
        >
          <RotateCcw className="mr-2 h-4 w-4" />
          Reset
        </Button>
        <Button onClick={handleSave} disabled={!hasChanges || updateMutation.isPending}>
          {updateMutation.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Save className="mr-2 h-4 w-4" />
          )}
          Save Changes
        </Button>
      </div>
    </div>
  );
}


// =============================================================================
// Sub-components
// =============================================================================

interface StatusCardProps {
  qaStatus?: {
    enabled: boolean;
    auto_evaluate: boolean;
    evaluation_model: string;
    default_threshold: number;
    api_key_configured: boolean;
  };
  isLoading: boolean;
}

function StatusCard({ qaStatus, isLoading }: StatusCardProps) {
  if (isLoading) {
    return (
      <Card>
        <CardContent className="p-4">
          <Skeleton className="h-16 w-full" />
        </CardContent>
      </Card>
    );
  }

  const isHealthy = qaStatus?.enabled && qaStatus?.api_key_configured;
  const statusColor = isHealthy ? "border-green-500/20 bg-green-500/5" : "border-yellow-500/20 bg-yellow-500/5";
  const statusIcon = isHealthy ? (
    <CheckCircle className="h-5 w-5 text-green-500" />
  ) : (
    <AlertTriangle className="h-5 w-5 text-yellow-500" />
  );

  return (
    <Card className={statusColor}>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-background">
              <Shield className="h-5 w-5 text-primary" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium">QA Status</span>
                {statusIcon}
              </div>
              <p className="text-sm text-muted-foreground">
                {qaStatus?.enabled ? "Enabled" : "Disabled"}
                {qaStatus?.enabled && !qaStatus?.api_key_configured && " (API key not configured)"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4 text-sm">
            <div className="text-right">
              <p className="text-xs text-muted-foreground">API Key</p>
              <div className="flex items-center gap-1">
                {qaStatus?.api_key_configured ? (
                  <>
                    <CheckCircle className="h-3 w-3 text-green-500" />
                    <span className="text-green-600">Configured</span>
                  </>
                ) : (
                  <>
                    <XCircle className="h-3 w-3 text-red-500" />
                    <span className="text-red-600">Missing</span>
                  </>
                )}
              </div>
            </div>
            <div className="text-right">
              <p className="text-xs text-muted-foreground">Model</p>
              <p className="font-mono text-xs">{qaStatus?.evaluation_model?.split("-").slice(0, 2).join("-") ?? "N/A"}</p>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

interface SettingRowProps {
  label: string;
  description: string;
  disabled?: boolean;
  tooltip?: string;
  children: React.ReactNode;
}

function SettingRow({ label, description, disabled, tooltip, children }: SettingRowProps) {
  return (
    <div className="flex items-center justify-between">
      <div className="space-y-0.5">
        <div className="flex items-center gap-2">
          <Label className={disabled ? "text-muted-foreground" : ""}>{label}</Label>
          {tooltip && <InfoTooltip content={tooltip} />}
        </div>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      {children}
    </div>
  );
}

export default QASettingsPanel;
