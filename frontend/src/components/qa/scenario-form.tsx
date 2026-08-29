"use client";

import { useState } from "react";
import { useForm, useFieldArray } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Checkbox } from "@/components/ui/checkbox";
import { Loader2, Plus, Trash2, X, GripVertical } from "lucide-react";
import {
  createScenario,
  updateScenario,
  type TestScenario,
  type TestScenarioCreate,
  type TestScenarioUpdate,
} from "@/lib/api/qa";
import { InfoTooltip } from "@/components/ui/info-tooltip";

// =============================================================================
// Schema
// =============================================================================

const conversationTurnSchema = z.object({
  role: z.enum(["caller", "agent"]),
  message: z.string().min(1, "Message is required"),
  expected_response: z.string().optional(),
});

const scenarioFormSchema = z.object({
  name: z.string().min(1, "Name is required").max(200, "Name too long"),
  description: z.string().optional(),
  category: z.string().min(1, "Category is required"),
  difficulty: z.string().min(1, "Difficulty is required"),
  tags: z.array(z.string()).optional(),
  // Caller Persona
  caller_name: z.string().optional(),
  caller_mood: z.string().optional(),
  caller_goal: z.string().optional(),
  caller_context: z.string().optional(),
  // Conversation Flow
  conversation_flow: z.array(conversationTurnSchema).min(1, "At least one turn is required"),
  // Expected Behaviors
  expected_behaviors: z.array(z.string()).optional(),
  // Success Criteria
  success_criteria_items: z
    .array(
      z.object({
        criterion: z.string().min(1),
        required: z.boolean(),
      })
    )
    .optional(),
});

type ScenarioFormValues = z.infer<typeof scenarioFormSchema>;

// =============================================================================
// Constants
// =============================================================================

const CATEGORIES = [
  { value: "greeting", label: "Greeting" },
  { value: "booking", label: "Booking" },
  { value: "inquiry", label: "Inquiry" },
  { value: "objection", label: "Objection" },
  { value: "support", label: "Support" },
  { value: "sales", label: "Sales" },
  { value: "custom", label: "Custom" },
];

const DIFFICULTIES = [
  { value: "easy", label: "Easy" },
  { value: "medium", label: "Medium" },
  { value: "hard", label: "Hard" },
];

const MOODS = [
  { value: "neutral", label: "Neutral" },
  { value: "friendly", label: "Friendly" },
  { value: "impatient", label: "Impatient" },
  { value: "frustrated", label: "Frustrated" },
  { value: "confused", label: "Confused" },
  { value: "angry", label: "Angry" },
];

// =============================================================================
// Types
// =============================================================================

interface ScenarioFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scenario?: TestScenario | null;
  workspaceId?: string;
  mode?: "create" | "edit" | "view";
}

// =============================================================================
// Helpers
// =============================================================================

function scenarioToFormValues(scenario: TestScenario): ScenarioFormValues {
  const persona = scenario.caller_persona as Record<string, unknown> | undefined;
  const criteria = scenario.success_criteria as Record<string, unknown> | undefined;
  const scenarioWithExtras = scenario as {
    tags?: string[];
    conversation_flow?: Array<{ role: string; message: string; expected_response?: string }>;
  };

  return {
    name: scenario.name,
    description: scenario.description ?? "",
    category: scenario.category,
    difficulty: scenario.difficulty,
    tags: scenarioWithExtras.tags ?? [],
    caller_name: String(persona?.name ?? ""),
    caller_mood: String(persona?.mood ?? ""),
    caller_goal: String(persona?.goal ?? ""),
    caller_context: String(persona?.context ?? ""),
    conversation_flow: (scenarioWithExtras.conversation_flow ?? []).map((turn) => ({
      role: turn.role as "caller" | "agent",
      message: turn.message,
      expected_response: turn.expected_response,
    })),
    expected_behaviors: scenario.expected_behaviors ?? [],
    success_criteria_items:
      (criteria?.items as Array<{ criterion: string; required: boolean }>) ?? [],
  };
}

function formValuesToCreatePayload(
  values: ScenarioFormValues,
  workspaceId?: string
): TestScenarioCreate {
  return {
    name: values.name,
    description: values.description ?? null,
    category: values.category,
    difficulty: values.difficulty,
    tags: values.tags?.length ? values.tags : null,
    caller_persona: {
      name: values.caller_name ?? "Caller",
      mood: values.caller_mood ?? "neutral",
      goal: values.caller_goal ?? "",
      context: values.caller_context ?? "",
    },
    conversation_flow: values.conversation_flow.map((turn) => ({
      role: turn.role,
      message: turn.message,
      expected_response: turn.expected_response ?? null,
    })),
    expected_behaviors: values.expected_behaviors?.filter(Boolean) ?? [],
    success_criteria: {
      items: values.success_criteria_items?.filter((item) => item.criterion) ?? [],
    },
    workspace_id: workspaceId ?? null,
  };
}

function formValuesToUpdatePayload(values: ScenarioFormValues): TestScenarioUpdate {
  return {
    name: values.name,
    description: values.description ?? null,
    category: values.category,
    difficulty: values.difficulty,
    tags: values.tags?.length ? values.tags : null,
    caller_persona: {
      name: values.caller_name ?? "Caller",
      mood: values.caller_mood ?? "neutral",
      goal: values.caller_goal ?? "",
      context: values.caller_context ?? "",
    },
    conversation_flow: values.conversation_flow.map((turn) => ({
      role: turn.role,
      message: turn.message,
      expected_response: turn.expected_response ?? null,
    })),
    expected_behaviors: values.expected_behaviors?.filter(Boolean) ?? [],
    success_criteria: {
      items: values.success_criteria_items?.filter((item) => item.criterion) ?? [],
    },
  };
}

// =============================================================================
// Component
// =============================================================================

export function ScenarioForm({
  open,
  onOpenChange,
  scenario,
  workspaceId,
  mode = "create",
}: ScenarioFormProps) {
  const queryClient = useQueryClient();
  const [tagInput, setTagInput] = useState("");
  const isViewMode = mode === "view";
  const isEditMode = mode === "edit";

  const defaultValues: ScenarioFormValues = scenario
    ? scenarioToFormValues(scenario)
    : {
        name: "",
        description: "",
        category: "custom",
        difficulty: "medium",
        tags: [],
        caller_name: "",
        caller_mood: "neutral",
        caller_goal: "",
        caller_context: "",
        conversation_flow: [{ role: "caller", message: "" }],
        expected_behaviors: [],
        success_criteria_items: [],
      };

  const form = useForm<ScenarioFormValues>({
    resolver: zodResolver(scenarioFormSchema),
    defaultValues,
  });

  const {
    fields: flowFields,
    append: appendFlow,
    remove: removeFlow,
  } = useFieldArray({
    control: form.control,
    name: "conversation_flow",
  });

  const {
    fields: criteriaFields,
    append: appendCriteria,
    remove: removeCriteria,
  } = useFieldArray({
    control: form.control,
    name: "success_criteria_items",
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (data: TestScenarioCreate) => createScenario(data),
    onSuccess: () => {
      toast.success("Scenario created successfully");
      void queryClient.invalidateQueries({ queryKey: ["qa-scenarios"] });
      onOpenChange(false);
      form.reset();
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to create scenario");
    },
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: (data: TestScenarioUpdate) => {
      if (!scenario) throw new Error("No scenario to update");
      return updateScenario(scenario.id, data);
    },
    onSuccess: () => {
      toast.success("Scenario updated successfully");
      void queryClient.invalidateQueries({ queryKey: ["qa-scenarios"] });
      onOpenChange(false);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to update scenario");
    },
  });

  const onSubmit = async (values: ScenarioFormValues) => {
    if (isEditMode && scenario) {
      updateMutation.mutate(formValuesToUpdatePayload(values));
    } else {
      createMutation.mutate(formValuesToCreatePayload(values, workspaceId));
    }
  };

  const handleAddTag = () => {
    const tag = tagInput.trim().toLowerCase();
    if (tag && !form.getValues("tags")?.includes(tag)) {
      form.setValue("tags", [...(form.getValues("tags") ?? []), tag]);
      setTagInput("");
    }
  };

  const handleRemoveTag = (tagToRemove: string) => {
    form.setValue("tags", form.getValues("tags")?.filter((t) => t !== tagToRemove) ?? []);
  };

  const isLoading = createMutation.isPending || updateMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {isViewMode ? "View Scenario" : isEditMode ? "Edit Scenario" : "Create Test Scenario"}
          </DialogTitle>
          <DialogDescription>
            {isViewMode
              ? "View the details of this test scenario"
              : isEditMode
                ? "Update the test scenario configuration"
                : "Create a new test scenario for your voice agent"}
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={(e) => void form.handleSubmit(onSubmit)(e)} className="space-y-6">
            <Accordion
              type="multiple"
              defaultValue={["basic", "persona", "flow", "criteria"]}
              className="w-full"
            >
              {/* Basic Info Section */}
              <AccordionItem value="basic">
                <AccordionTrigger>Basic Information</AccordionTrigger>
                <AccordionContent className="space-y-4 pt-4">
                  <FormField
                    control={form.control}
                    name="name"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Name</FormLabel>
                        <FormControl>
                          <Input
                            placeholder="e.g., VIP Client Booking Request"
                            {...field}
                            disabled={isViewMode}
                          />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <FormField
                    control={form.control}
                    name="description"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Description</FormLabel>
                        <FormControl>
                          <Textarea
                            placeholder="Describe what this scenario tests..."
                            {...field}
                            disabled={isViewMode}
                          />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <div className="grid grid-cols-2 gap-4">
                    <FormField
                      control={form.control}
                      name="category"
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>Category</FormLabel>
                          <Select
                            onValueChange={field.onChange}
                            defaultValue={field.value}
                            disabled={isViewMode}
                          >
                            <FormControl>
                              <SelectTrigger>
                                <SelectValue placeholder="Select category" />
                              </SelectTrigger>
                            </FormControl>
                            <SelectContent>
                              {CATEGORIES.map((cat) => (
                                <SelectItem key={cat.value} value={cat.value}>
                                  {cat.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <FormMessage />
                        </FormItem>
                      )}
                    />

                    <FormField
                      control={form.control}
                      name="difficulty"
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>Difficulty</FormLabel>
                          <Select
                            onValueChange={field.onChange}
                            defaultValue={field.value}
                            disabled={isViewMode}
                          >
                            <FormControl>
                              <SelectTrigger>
                                <SelectValue placeholder="Select difficulty" />
                              </SelectTrigger>
                            </FormControl>
                            <SelectContent>
                              {DIFFICULTIES.map((diff) => (
                                <SelectItem key={diff.value} value={diff.value}>
                                  {diff.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  </div>

                  {/* Tags */}
                  <FormField
                    control={form.control}
                    name="tags"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Tags</FormLabel>
                        <div className="mb-2 flex flex-wrap gap-2">
                          {field.value?.map((tag) => (
                            <Badge key={tag} variant="secondary" className="gap-1">
                              {tag}
                              {!isViewMode && (
                                <button
                                  type="button"
                                  onClick={() => handleRemoveTag(tag)}
                                  className="ml-1 hover:text-destructive"
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              )}
                            </Badge>
                          ))}
                        </div>
                        {!isViewMode && (
                          <div className="flex gap-2">
                            <Input
                              placeholder="Add tag..."
                              value={tagInput}
                              onChange={(e) => setTagInput(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  handleAddTag();
                                }
                              }}
                            />
                            <Button type="button" variant="outline" onClick={handleAddTag}>
                              Add
                            </Button>
                          </div>
                        )}
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </AccordionContent>
              </AccordionItem>

              {/* Caller Persona Section */}
              <AccordionItem value="persona">
                <AccordionTrigger>
                  <span className="flex items-center gap-2">Caller Persona</span>
                </AccordionTrigger>
                <AccordionContent className="space-y-4 pt-4">
                  <p className="flex items-center gap-2 text-[0.8rem] text-muted-foreground">
                    Define who the simulated caller is - their name, emotional state, goals, and
                    background context.
                    <InfoTooltip content="The caller persona helps the AI understand how to behave during the test. A frustrated caller will be more demanding, while a friendly caller will be more patient." />
                  </p>
                  <div className="grid grid-cols-2 gap-4">
                    <FormField
                      control={form.control}
                      name="caller_name"
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>Caller Name</FormLabel>
                          <FormControl>
                            <Input
                              placeholder="e.g., Sarah Johnson"
                              {...field}
                              disabled={isViewMode}
                            />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />

                    <FormField
                      control={form.control}
                      name="caller_mood"
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>Mood</FormLabel>
                          <Select
                            onValueChange={field.onChange}
                            defaultValue={field.value}
                            disabled={isViewMode}
                          >
                            <FormControl>
                              <SelectTrigger>
                                <SelectValue placeholder="Select mood" />
                              </SelectTrigger>
                            </FormControl>
                            <SelectContent>
                              {MOODS.map((mood) => (
                                <SelectItem key={mood.value} value={mood.value}>
                                  {mood.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  </div>

                  <FormField
                    control={form.control}
                    name="caller_goal"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Goal</FormLabel>
                        <FormControl>
                          <Input
                            placeholder="e.g., Book an urgent appointment for tomorrow"
                            {...field}
                            disabled={isViewMode}
                          />
                        </FormControl>
                        <FormDescription>What is the caller trying to achieve?</FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <FormField
                    control={form.control}
                    name="caller_context"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Context</FormLabel>
                        <FormControl>
                          <Textarea
                            placeholder="e.g., VIP client, has booked 10+ times before, prefers morning appointments"
                            {...field}
                            disabled={isViewMode}
                          />
                        </FormControl>
                        <FormDescription>Background information about the caller</FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </AccordionContent>
              </AccordionItem>

              {/* Conversation Flow Section */}
              <AccordionItem value="flow">
                <AccordionTrigger>
                  <span className="flex items-center gap-2">Conversation Flow</span>
                </AccordionTrigger>
                <AccordionContent className="space-y-4 pt-4">
                  <p className="flex items-center gap-2 text-[0.8rem] text-muted-foreground">
                    Define the expected conversation turns between caller and agent.
                    <InfoTooltip content="Script the expected conversation between the caller and your agent. Each turn alternates between what the caller says and how the agent should respond. This guides the test simulation." />
                  </p>

                  {flowFields.map((field, index) => (
                    <div key={field.id} className="flex items-start gap-2 rounded-lg border p-3">
                      <GripVertical className="mt-2 h-4 w-4 text-muted-foreground" />
                      <div className="flex-1 space-y-3">
                        <div className="flex items-center gap-2">
                          <Badge variant={field.role === "caller" ? "default" : "secondary"}>
                            Turn {index + 1}: {field.role === "caller" ? "Caller" : "Agent"}
                          </Badge>
                          {!isViewMode && (
                            <Select
                              value={form.watch(`conversation_flow.${index}.role`)}
                              onValueChange={(value) =>
                                form.setValue(
                                  `conversation_flow.${index}.role`,
                                  value as "caller" | "agent"
                                )
                              }
                            >
                              <SelectTrigger className="h-7 w-[100px]">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="caller">Caller</SelectItem>
                                <SelectItem value="agent">Agent</SelectItem>
                              </SelectContent>
                            </Select>
                          )}
                        </div>

                        <FormField
                          control={form.control}
                          name={`conversation_flow.${index}.message`}
                          render={({ field }) => (
                            <FormItem>
                              <FormControl>
                                <Textarea
                                  placeholder={
                                    form.watch(`conversation_flow.${index}.role`) === "caller"
                                      ? "What the caller says..."
                                      : "Expected agent response..."
                                  }
                                  {...field}
                                  disabled={isViewMode}
                                  className="min-h-[60px]"
                                />
                              </FormControl>
                              <FormMessage />
                            </FormItem>
                          )}
                        />
                      </div>

                      {!isViewMode && flowFields.length > 1 && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          onClick={() => removeFlow(index)}
                          className="h-8 w-8 text-muted-foreground hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      )}
                    </div>
                  ))}

                  {!isViewMode && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => appendFlow({ role: "caller", message: "" })}
                    >
                      <Plus className="mr-2 h-4 w-4" />
                      Add Turn
                    </Button>
                  )}
                </AccordionContent>
              </AccordionItem>

              {/* Success Criteria Section */}
              <AccordionItem value="criteria">
                <AccordionTrigger>
                  <span className="flex items-center gap-2">Success Criteria</span>
                </AccordionTrigger>
                <AccordionContent className="space-y-4 pt-4">
                  <p className="flex items-center gap-2 text-[0.8rem] text-muted-foreground">
                    Define what the agent must do for this scenario to pass. Check the box to mark a
                    criterion as required.
                    <InfoTooltip content="Required criteria must be met for the test to pass. Optional criteria are tracked but won't cause a failure if not met." />
                  </p>

                  {criteriaFields.map((field, index) => (
                    <div key={field.id} className="flex items-center gap-2">
                      <FormField
                        control={form.control}
                        name={`success_criteria_items.${index}.required`}
                        render={({ field }) => (
                          <FormItem className="flex items-center space-x-2 space-y-0">
                            <FormControl>
                              <Checkbox
                                checked={field.value}
                                onCheckedChange={field.onChange}
                                disabled={isViewMode}
                              />
                            </FormControl>
                          </FormItem>
                        )}
                      />

                      <FormField
                        control={form.control}
                        name={`success_criteria_items.${index}.criterion`}
                        render={({ field }) => (
                          <FormItem className="flex-1">
                            <FormControl>
                              <Input
                                placeholder="e.g., Agent must confirm booking details"
                                {...field}
                                disabled={isViewMode}
                              />
                            </FormControl>
                            <FormMessage />
                          </FormItem>
                        )}
                      />

                      {!isViewMode && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          onClick={() => removeCriteria(index)}
                          className="h-8 w-8 text-muted-foreground hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      )}
                    </div>
                  ))}

                  {!isViewMode && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => appendCriteria({ criterion: "", required: true })}
                    >
                      <Plus className="mr-2 h-4 w-4" />
                      Add Criterion
                    </Button>
                  )}
                </AccordionContent>
              </AccordionItem>
            </Accordion>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                {isViewMode ? "Close" : "Cancel"}
              </Button>
              {!isViewMode && (
                <Button type="submit" disabled={isLoading}>
                  {isLoading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {isEditMode ? "Update Scenario" : "Create Scenario"}
                </Button>
              )}
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}

export default ScenarioForm;
