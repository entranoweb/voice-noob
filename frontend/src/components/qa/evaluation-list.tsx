"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { CheckCircle, XCircle } from "lucide-react";
import type { CallEvaluation } from "@/lib/api/qa";

interface EvaluationListProps {
  evaluations: CallEvaluation[];
  showCallLink?: boolean;
}

export function EvaluationList({ evaluations, showCallLink = true }: EvaluationListProps) {
  const getScoreColor = (score: number) => {
    if (score >= 80) return "text-green-600";
    if (score >= 60) return "text-yellow-600";
    return "text-red-600";
  };

  const getScoreBgColor = (score: number) => {
    if (score >= 80) return "bg-green-100 text-green-800";
    if (score >= 60) return "bg-yellow-100 text-yellow-800";
    return "bg-red-100 text-red-800";
  };

  if (evaluations.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-center">
        <CheckCircle className="mb-2 h-8 w-8 text-muted-foreground/30" />
        <p className="text-sm text-muted-foreground">No evaluations found</p>
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Status</TableHead>
          <TableHead>Score</TableHead>
          <TableHead>Intent</TableHead>
          <TableHead>Tools</TableHead>
          <TableHead>Compliance</TableHead>
          <TableHead>Quality</TableHead>
          <TableHead>Date</TableHead>
          {showCallLink && <TableHead>Call</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {evaluations.map((evaluation) => (
          <TableRow key={evaluation.id}>
            <TableCell>
              <div className="flex items-center gap-2">
                {evaluation.passed ? (
                  <Badge className="bg-green-100 text-green-800 hover:bg-green-100">
                    <CheckCircle className="mr-1 h-3 w-3" />
                    Pass
                  </Badge>
                ) : (
                  <Badge className="bg-red-100 text-red-800 hover:bg-red-100">
                    <XCircle className="mr-1 h-3 w-3" />
                    Fail
                  </Badge>
                )}
              </div>
            </TableCell>
            <TableCell>
              <Badge className={getScoreBgColor(evaluation.overall_score)}>
                {evaluation.overall_score}
              </Badge>
            </TableCell>
            <TableCell>
              <span className={getScoreColor(evaluation.intent_completion ?? 0)}>
                {evaluation.intent_completion ?? "N/A"}
              </span>
            </TableCell>
            <TableCell>
              <span className={getScoreColor(evaluation.tool_usage ?? 0)}>
                {evaluation.tool_usage ?? "N/A"}
              </span>
            </TableCell>
            <TableCell>
              <span className={getScoreColor(evaluation.compliance ?? 0)}>
                {evaluation.compliance ?? "N/A"}
              </span>
            </TableCell>
            <TableCell>
              <span className={getScoreColor(evaluation.response_quality ?? 0)}>
                {evaluation.response_quality ?? "N/A"}
              </span>
            </TableCell>
            <TableCell className="text-muted-foreground">
              {new Date(evaluation.created_at).toLocaleDateString()}
            </TableCell>
            {showCallLink && (
              <TableCell>
                <Link
                  href={`/dashboard/calls/${evaluation.call_id}`}
                  className="text-primary hover:underline"
                >
                  View
                </Link>
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
