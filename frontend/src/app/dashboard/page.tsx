import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Bot, Phone, TrendingUp, Clock } from "lucide-react";

export default function DashboardPage() {
  const stats = [
    {
      title: "Active Agents",
      value: "0",
      description: "Voice agents running",
      icon: Bot,
      trend: "+0%",
    },
    {
      title: "Total Calls",
      value: "0",
      description: "Last 30 days",
      icon: Phone,
      trend: "+0%",
    },
    {
      title: "Avg Duration",
      value: "0m",
      description: "Per call",
      icon: Clock,
      trend: "+0%",
    },
    {
      title: "Success Rate",
      value: "0%",
      description: "Completed calls",
      icon: TrendingUp,
      trend: "+0%",
    },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-muted-foreground">Overview of your voice agent platform</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {stats.map((stat) => (
          <Card key={stat.title}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{stat.title}</CardTitle>
              <stat.icon className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{stat.value}</div>
              <p className="text-xs text-muted-foreground">{stat.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-7">
        <Card className="col-span-4">
          <CardHeader>
            <CardTitle>Recent Activity</CardTitle>
            <CardDescription>
              No recent calls yet. Create your first voice agent to get started.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex h-[300px] items-center justify-center text-muted-foreground">
            <div className="text-center">
              <Bot className="mx-auto mb-4 h-12 w-12 opacity-50" />
              <p>Create your first voice agent to see activity</p>
            </div>
          </CardContent>
        </Card>

        <Card className="col-span-3">
          <CardHeader>
            <CardTitle>Quick Actions</CardTitle>
            <CardDescription>Get started with your platform</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <a
              href="/dashboard/agents/new-simplified"
              className="block rounded-lg border p-4 transition-colors hover:bg-accent"
            >
              <div className="font-medium">Create Voice Agent</div>
              <div className="text-sm text-muted-foreground">
                Choose pricing tier & configure your agent
              </div>
            </a>
            <a
              href="/dashboard/phone-numbers"
              className="block rounded-lg border p-4 transition-colors hover:bg-accent"
            >
              <div className="font-medium">Get Phone Number</div>
              <div className="text-sm text-muted-foreground">Purchase a number for your agents</div>
            </a>
            <a
              href="/dashboard/settings"
              className="block rounded-lg border p-4 transition-colors hover:bg-accent"
            >
              <div className="font-medium">Configure API Keys</div>
              <div className="text-sm text-muted-foreground">
                Add OpenAI, Deepgram, ElevenLabs keys
              </div>
            </a>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
