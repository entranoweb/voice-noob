"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Phone, Plus, MoreVertical, Search, Loader2 } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { toast } from "sonner";

type PhoneNumber = {
  id: string;
  phoneNumber: string;
  provider: string;
  agentName?: string;
  isActive: boolean;
};

type AvailableNumber = {
  phone_number: string;
  locality?: string;
  region?: string;
  monthly_cost: string;
};

export default function PhoneNumbersPage() {
  // State for modals
  const [isPurchaseModalOpen, setIsPurchaseModalOpen] = useState(false);
  const [isDetailsModalOpen, setIsDetailsModalOpen] = useState(false);
  const [isAssignModalOpen, setIsAssignModalOpen] = useState(false);
  const [selectedNumber, setSelectedNumber] = useState<PhoneNumber | null>(null);

  // Purchase flow state
  const [searchAreaCode, setSearchAreaCode] = useState("");
  const [selectedProvider, setSelectedProvider] = useState<string>("telnyx");
  const [isSearching, setIsSearching] = useState(false);
  const [availableNumbers, setAvailableNumbers] = useState<AvailableNumber[]>([]);
  const [selectedAvailableNumber, setSelectedAvailableNumber] = useState<string | null>(null);
  const [isPurchasing, setIsPurchasing] = useState(false);

  // Mock data - will be replaced with API call
  const phoneNumbers: PhoneNumber[] = [];

  // Mock agents for assignment
  const agents = [
    { id: "1", name: "Customer Support Agent" },
    { id: "2", name: "Sales Agent" },
  ];

  const openPurchaseModal = () => {
    setSearchAreaCode("");
    setAvailableNumbers([]);
    setSelectedAvailableNumber(null);
    setIsPurchaseModalOpen(true);
  };

  const searchAvailableNumbers = async () => {
    if (!searchAreaCode || searchAreaCode.length < 3) {
      toast.error("Please enter at least 3 digits for area code");
      return;
    }

    setIsSearching(true);
    try {
      // Simulate API call - replace with actual API call
      await new Promise((resolve) => setTimeout(resolve, 1500));

      // Mock available numbers
      const mockNumbers: AvailableNumber[] = [
        {
          phone_number: `+1${searchAreaCode}5551234`,
          locality: "San Francisco",
          region: "CA",
          monthly_cost: "$1.00",
        },
        {
          phone_number: `+1${searchAreaCode}5555678`,
          locality: "Los Angeles",
          region: "CA",
          monthly_cost: "$1.00",
        },
        {
          phone_number: `+1${searchAreaCode}5559012`,
          locality: "New York",
          region: "NY",
          monthly_cost: "$1.00",
        },
      ];
      setAvailableNumbers(mockNumbers);
    } catch {
      toast.error("Failed to search for available numbers");
    } finally {
      setIsSearching(false);
    }
  };

  const purchaseNumber = async () => {
    if (!selectedAvailableNumber) {
      toast.error("Please select a number to purchase");
      return;
    }

    setIsPurchasing(true);
    try {
      // Simulate API call - replace with actual API call
      await new Promise((resolve) => setTimeout(resolve, 2000));
      toast.success(`Successfully purchased ${selectedAvailableNumber}`);
      setIsPurchaseModalOpen(false);
    } catch {
      toast.error("Failed to purchase number");
    } finally {
      setIsPurchasing(false);
    }
  };

  const openDetailsModal = (number: PhoneNumber) => {
    setSelectedNumber(number);
    setIsDetailsModalOpen(true);
  };

  const openAssignModal = (number: PhoneNumber) => {
    setSelectedNumber(number);
    setIsAssignModalOpen(true);
  };

  const handleAssignAgent = async (agentId: string) => {
    try {
      // Simulate API call
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const agent = agents.find((a) => a.id === agentId);
      toast.success(`Assigned ${selectedNumber?.phoneNumber} to ${agent?.name}`);
      setIsAssignModalOpen(false);
    } catch {
      toast.error("Failed to assign agent");
    }
  };

  const handleReleaseNumber = async (number: PhoneNumber) => {
    if (
      !confirm(
        `Are you sure you want to release ${number.phoneNumber}? This action cannot be undone.`
      )
    ) {
      return;
    }

    try {
      // Simulate API call
      await new Promise((resolve) => setTimeout(resolve, 1000));
      toast.success(`Released ${number.phoneNumber}`);
    } catch {
      toast.error("Failed to release number");
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Phone Numbers</h1>
          <p className="text-muted-foreground">Manage phone numbers for your voice agents</p>
        </div>
        <Button onClick={openPurchaseModal}>
          <Plus className="mr-2 h-4 w-4" />
          Purchase Number
        </Button>
      </div>

      {phoneNumbers.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-16">
            <Phone className="mb-4 h-16 w-16 text-muted-foreground/50" />
            <h3 className="mb-2 text-lg font-semibold">No phone numbers yet</h3>
            <p className="mb-4 max-w-sm text-center text-sm text-muted-foreground">
              Purchase a phone number from Telnyx or Twilio to start receiving calls
            </p>
            <Button onClick={openPurchaseModal}>
              <Plus className="mr-2 h-4 w-4" />
              Purchase Your First Number
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Your Phone Numbers</CardTitle>
            <CardDescription>{phoneNumbers.length} number(s) available</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Phone Number</TableHead>
                  <TableHead>Provider</TableHead>
                  <TableHead>Assigned Agent</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="w-[70px]"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {phoneNumbers.map((number) => (
                  <TableRow key={number.id}>
                    <TableCell className="font-mono font-medium">{number.phoneNumber}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{number.provider}</Badge>
                    </TableCell>
                    <TableCell>
                      {number.agentName ?? (
                        <span className="text-muted-foreground">Unassigned</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={number.isActive ? "default" : "secondary"}>
                        {number.isActive ? "Active" : "Inactive"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="icon">
                            <MoreVertical className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem onClick={() => openAssignModal(number)}>
                            Assign to Agent
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => openDetailsModal(number)}>
                            View Details
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            className="text-destructive"
                            onClick={() => void handleReleaseNumber(number)}
                          >
                            Release Number
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* Purchase Number Modal */}
      <Dialog open={isPurchaseModalOpen} onOpenChange={setIsPurchaseModalOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <DialogHeader>
            <DialogTitle>Purchase Phone Number</DialogTitle>
            <DialogDescription>Search for available phone numbers by area code</DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="provider">Provider</Label>
              <Select value={selectedProvider} onValueChange={setSelectedProvider}>
                <SelectTrigger>
                  <SelectValue placeholder="Select provider" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="telnyx">Telnyx</SelectItem>
                  <SelectItem value="twilio">Twilio</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="areaCode">Area Code</Label>
              <div className="flex gap-2">
                <Input
                  id="areaCode"
                  placeholder="e.g., 415"
                  value={searchAreaCode}
                  onChange={(e) => setSearchAreaCode(e.target.value.replace(/\D/g, "").slice(0, 3))}
                  maxLength={3}
                />
                <Button onClick={() => void searchAvailableNumbers()} disabled={isSearching}>
                  {isSearching ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Search className="h-4 w-4" />
                  )}
                </Button>
              </div>
            </div>

            {availableNumbers.length > 0 && (
              <div className="space-y-2">
                <Label>Available Numbers</Label>
                <div className="max-h-[200px] space-y-2 overflow-y-auto rounded-md border p-2">
                  {availableNumbers.map((num) => (
                    <div
                      key={num.phone_number}
                      onClick={() => setSelectedAvailableNumber(num.phone_number)}
                      className={`flex cursor-pointer items-center justify-between rounded-md p-2 transition-colors ${
                        selectedAvailableNumber === num.phone_number
                          ? "bg-primary text-primary-foreground"
                          : "hover:bg-accent"
                      }`}
                    >
                      <div>
                        <p className="font-mono font-medium">{num.phone_number}</p>
                        <p className="text-xs opacity-80">
                          {num.locality}, {num.region}
                        </p>
                      </div>
                      <span className="text-sm">{num.monthly_cost}/mo</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setIsPurchaseModalOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => void purchaseNumber()}
              disabled={!selectedAvailableNumber || isPurchasing}
            >
              {isPurchasing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Purchase Number
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Details Modal */}
      <Dialog open={isDetailsModalOpen} onOpenChange={setIsDetailsModalOpen}>
        <DialogContent className="sm:max-w-[400px]">
          <DialogHeader>
            <DialogTitle>Phone Number Details</DialogTitle>
          </DialogHeader>
          {selectedNumber && (
            <div className="space-y-4 py-4">
              <div className="space-y-1">
                <Label className="text-muted-foreground">Phone Number</Label>
                <p className="font-mono text-lg font-medium">{selectedNumber.phoneNumber}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Provider</Label>
                <p>{selectedNumber.provider}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Assigned Agent</Label>
                <p>{selectedNumber.agentName ?? "Unassigned"}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Status</Label>
                <Badge variant={selectedNumber.isActive ? "default" : "secondary"}>
                  {selectedNumber.isActive ? "Active" : "Inactive"}
                </Badge>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsDetailsModalOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Assign Agent Modal */}
      <Dialog open={isAssignModalOpen} onOpenChange={setIsAssignModalOpen}>
        <DialogContent className="sm:max-w-[400px]">
          <DialogHeader>
            <DialogTitle>Assign to Agent</DialogTitle>
            <DialogDescription>
              Select an agent to handle calls on {selectedNumber?.phoneNumber}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Select Agent</Label>
              <Select onValueChange={(value) => void handleAssignAgent(value)}>
                <SelectTrigger>
                  <SelectValue placeholder="Choose an agent" />
                </SelectTrigger>
                <SelectContent>
                  {agents.map((agent) => (
                    <SelectItem key={agent.id} value={agent.id}>
                      {agent.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsAssignModalOpen(false)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
