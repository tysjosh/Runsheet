import { ApiResponse, Truck, FleetSummary, FleetFilters } from '../types/api';

// API base URL - replace with actual API endpoint
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api';

// Timeout configuration (in milliseconds)
// Requirement 9.4: Configurable timeouts - 30s for standard calls, 120s for AI streaming
export const API_TIMEOUTS = {
  STANDARD: 30000,      // 30 seconds for standard API calls
  AI_STREAMING: 120000, // 120 seconds for AI streaming responses
} as const;

// Custom error class for timeout errors
export class ApiTimeoutError extends Error {
  constructor(message: string = 'Request timed out') {
    super(message);
    this.name = 'ApiTimeoutError';
  }
}

// Custom error class for API errors
export class ApiError extends Error {
  status: number;
  
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

// Helper function to create a fetch with timeout
async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUTS.STANDARD
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return response;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiTimeoutError(`Request timed out after ${timeout / 1000} seconds`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

// Types for other components
export interface InventoryItem {
  id: string;
  name: string;
  category: string;
  quantity: number;
  unit: string;
  location: string;
  status: 'in_stock' | 'low_stock' | 'out_of_stock';
  lastUpdated: string;
}

export interface Order {
  id: string;
  customer: string;
  status: 'pending' | 'in_transit' | 'delivered' | 'cancelled';
  value: number;
  items: string;
  truckId?: string;
  region: string;
  createdAt: string;
  deliveryEta: string;
  priority: 'low' | 'medium' | 'high' | 'urgent';
}

export interface SupportTicket {
  id: string;
  customer: string;
  issue: string;
  description: string;
  priority: 'low' | 'medium' | 'high' | 'urgent';
  status: 'open' | 'in_progress' | 'resolved' | 'closed';
  createdAt: string;
  assignedTo?: string;
  relatedOrder?: string;
}

export interface AnalyticsMetrics {
  delivery_performance: { title: string; value: string; change: string; trend: 'up' | 'down' };
  average_delay: { title: string; value: string; change: string; trend: 'up' | 'down' };
  fleet_utilization: { title: string; value: string; change: string; trend: 'up' | 'down' };
  customer_satisfaction: { title: string; value: string; change: string; trend: 'up' | 'down' };
}

class ApiService {
  private async request<T>(
    endpoint: string, 
    options?: RequestInit,
    timeout: number = API_TIMEOUTS.STANDARD
  ): Promise<ApiResponse<T>> {
    try {
      const response = await fetchWithTimeout(
        `${API_BASE_URL}${endpoint}`,
        {
          headers: {
            'Content-Type': 'application/json',
            ...options?.headers,
          },
          ...options,
        },
        timeout
      );

      if (!response.ok) {
        throw new ApiError(`HTTP error! status: ${response.status}`, response.status);
      }

      return await response.json();
    } catch (error) {
      if (error instanceof ApiTimeoutError) {
        console.error('API request timed out:', error.message);
        throw error;
      }
      if (error instanceof ApiError) {
        console.error('API request failed:', error.message);
        throw error;
      }
      console.error('API request failed:', error);
      throw error;
    }
  }

  // Fleet Management
  async getFleetSummary(): Promise<ApiResponse<FleetSummary>> {
    return this.request<FleetSummary>('/fleet/summary');
  }

  async getTrucks(filters?: FleetFilters): Promise<ApiResponse<Truck[]>> {
    const queryParams = filters ? `?${new URLSearchParams(filters as any).toString()}` : '';
    return this.request<Truck[]>(`/fleet/trucks${queryParams}`);
  }

  async getTruckById(id: string): Promise<ApiResponse<Truck>> {
    return this.request<Truck>(`/fleet/trucks/${id}`);
  }

  async updateTruckStatus(id: string, status: string): Promise<ApiResponse<Truck>> {
    return this.request<Truck>(`/fleet/trucks/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    });
  }

  // Inventory Management
  async getInventory(): Promise<ApiResponse<InventoryItem[]>> {
    return this.request<InventoryItem[]>('/inventory');
  }

  async getInventoryById(id: string): Promise<ApiResponse<InventoryItem>> {
    return this.request<InventoryItem>(`/inventory/${id}`);
  }

  async updateInventoryItem(id: string, data: Partial<InventoryItem>): Promise<ApiResponse<InventoryItem>> {
    return this.request<InventoryItem>(`/inventory/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  // Orders Management
  async getOrders(): Promise<ApiResponse<Order[]>> {
    return this.request<Order[]>('/orders');
  }

  async getOrderById(id: string): Promise<ApiResponse<Order>> {
    return this.request<Order>(`/orders/${id}`);
  }

  async createOrder(order: Omit<Order, 'id' | 'createdAt'>): Promise<ApiResponse<Order>> {
    return this.request<Order>('/orders', {
      method: 'POST',
      body: JSON.stringify(order),
    });
  }

  async updateOrderStatus(id: string, status: string): Promise<ApiResponse<Order>> {
    return this.request<Order>(`/orders/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    });
  }

  // Support Management
  async getSupportTickets(): Promise<ApiResponse<SupportTicket[]>> {
    return this.request<SupportTicket[]>('/support/tickets');
  }

  async getSupportTicketById(id: string): Promise<ApiResponse<SupportTicket>> {
    return this.request<SupportTicket>(`/support/tickets/${id}`);
  }

  async createSupportTicket(ticket: Omit<SupportTicket, 'id' | 'createdAt'>): Promise<ApiResponse<SupportTicket>> {
    return this.request<SupportTicket>('/support/tickets', {
      method: 'POST',
      body: JSON.stringify(ticket),
    });
  }

  async updateSupportTicket(id: string, data: Partial<SupportTicket>): Promise<ApiResponse<SupportTicket>> {
    return this.request<SupportTicket>(`/support/tickets/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  // Analytics
  async getAnalyticsMetrics(timeRange: string = '7d'): Promise<ApiResponse<AnalyticsMetrics>> {
    return this.request<AnalyticsMetrics>(`/analytics/metrics?timeRange=${timeRange}`);
  }

  async getAnalyticsRoutePerformance(): Promise<ApiResponse<any[]>> {
    return this.request<any[]>('/analytics/routes');
  }

  async getAnalyticsDelayCauses(): Promise<ApiResponse<any[]>> {
    return this.request<any[]>('/analytics/delay-causes');
  }

  async getAnalyticsRegionalPerformance(): Promise<ApiResponse<any[]>> {
    return this.request<any[]>('/analytics/regional');
  }

  // Data Upload - Legacy methods (keeping for compatibility)
  async uploadFromSheets(url: string, dataType: string): Promise<ApiResponse<{ recordCount: number }>> {
    return this.request<{ recordCount: number }>('/data/upload/sheets', {
      method: 'POST',
      body: JSON.stringify({ url, dataType }),
    });
  }

  async uploadCSV(file: File, dataType: string): Promise<ApiResponse<{ recordCount: number }>> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('dataType', dataType);

    return this.request<{ recordCount: number }>('/data/upload/csv', {
      method: 'POST',
      body: formData,
      headers: {}, // Let browser set Content-Type for FormData
    });
  }

  // Temporal Data Upload - New methods for demo
  async uploadTemporalCSV(
    file: File, 
    dataType: string, 
    batchId: string, 
    operationalTime: string
  ): Promise<ApiResponse<{ recordCount: number; batch_id: string; operational_time: string }>> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('data_type', dataType);
    formData.append('batch_id', batchId);
    formData.append('operational_time', operationalTime);

    return this.request<{ recordCount: number; batch_id: string; operational_time: string }>('/upload/csv', {
      method: 'POST',
      body: formData,
      headers: {}, // Let browser set Content-Type for FormData
    });
  }

  async uploadTemporalSheets(
    url: string, 
    dataType: string, 
    batchId: string, 
    operationalTime: string
  ): Promise<ApiResponse<{ recordCount: number; batch_id: string; operational_time: string }>> {
    return this.request<{ recordCount: number; batch_id: string; operational_time: string }>('/upload/sheets', {
      method: 'POST',
      body: JSON.stringify({ 
        data_type: dataType, 
        batch_id: batchId, 
        operational_time: operationalTime,
        sheets_url: url 
      }),
    });
  }

  async uploadBatchTemporal(
    batchId: string, 
    operationalTime: string
  ): Promise<ApiResponse<{ recordCount: number; batch_id: string; operational_time: string; breakdown: Record<string, number> }>> {
    return this.request<{ recordCount: number; batch_id: string; operational_time: string; breakdown: Record<string, number> }>('/upload/batch', {
      method: 'POST',
      body: JSON.stringify({ 
        batch_id: batchId, 
        operational_time: operationalTime
      }),
    });
  }

  async uploadSelectiveTemporal(
    dataTypes: string[],
    batchId: string, 
    operationalTime: string
  ): Promise<ApiResponse<{ recordCount: number; batch_id: string; operational_time: string; breakdown: Record<string, number> }>> {
    return this.request<{ recordCount: number; batch_id: string; operational_time: string; breakdown: Record<string, number> }>('/upload/selective', {
      method: 'POST',
      body: JSON.stringify({ 
        data_types: dataTypes,
        batch_id: batchId, 
        operational_time: operationalTime
      }),
    });
  }

  // Demo Management
  async resetDemo(): Promise<ApiResponse<{ state: string; message: string }>> {
    return this.request<{ state: string; message: string }>('/demo/reset', {
      method: 'POST',
    });
  }

  async getDemoStatus(): Promise<{ current_state: string; total_trucks: number; success: boolean; timestamp: string }> {
    const response = await fetchWithTimeout(
      `${API_BASE_URL}/demo/status`,
      {
        headers: {
          'Content-Type': 'application/json',
        },
      },
      API_TIMEOUTS.STANDARD
    );

    if (!response.ok) {
      throw new ApiError(`HTTP error! status: ${response.status}`, response.status);
    }

    return await response.json();
  }

  // Real-time updates
  // Note: For React components, use the useFleetWebSocket hook instead
  // This method is kept for backward compatibility
  async subscribeToFleetUpdates(callback: (data: Truck[]) => void): Promise<() => void> {
    // WebSocket connection for real-time updates with reconnection
    // For better reconnection handling, use the useFleetWebSocket hook in React components
    const wsUrl = `${API_BASE_URL.replace('http', 'ws')}/fleet/live`;
    
    let ws: WebSocket | null = null;
    let reconnectAttempt = 0;
    let reconnectTimeout: NodeJS.Timeout | null = null;
    let shouldReconnect = true;
    
    const INITIAL_RECONNECT_DELAY = 1000;  // 1 second
    const MAX_RECONNECT_DELAY = 30000;     // 30 seconds
    const BACKOFF_MULTIPLIER = 2;
    
    /**
     * Calculate exponential backoff delay with jitter
     * Validates: Requirement 9.5 - exponential backoff
     */
    const calculateBackoffDelay = (attempt: number): number => {
      const exponentialDelay = INITIAL_RECONNECT_DELAY * Math.pow(BACKOFF_MULTIPLIER, attempt - 1);
      const cappedDelay = Math.min(exponentialDelay, MAX_RECONNECT_DELAY);
      // Add jitter (±25%) to prevent thundering herd
      const jitter = cappedDelay * 0.25 * (Math.random() * 2 - 1);
      return Math.floor(cappedDelay + jitter);
    };
    
    /**
     * Connect to WebSocket with reconnection support
     */
    const connect = () => {
      if (!shouldReconnect) return;
      
      try {
        ws = new WebSocket(wsUrl);
        
        ws.onopen = () => {
          console.log('Fleet WebSocket connected');
          reconnectAttempt = 0;
        };
        
        ws.onmessage = (event) => {
          try {
            const message = JSON.parse(event.data);
            
            // Handle different message types
            if (message.type === 'location_update' && message.data) {
              // Convert single update to array format for callback
              callback([message.data as Truck]);
            } else if (message.type === 'batch_location_update' && message.data?.updates) {
              callback(message.data.updates as Truck[]);
            }
          } catch (error) {
            console.error('Failed to parse WebSocket message:', error);
          }
        };
        
        ws.onclose = (event) => {
          console.log('Fleet WebSocket disconnected', event.code, event.reason);
          
          // Reconnect if not a clean close and we should reconnect
          if (shouldReconnect && !event.wasClean) {
            reconnectAttempt++;
            const delay = calculateBackoffDelay(reconnectAttempt);
            console.log(`Reconnecting in ${delay}ms (attempt ${reconnectAttempt})`);
            
            reconnectTimeout = setTimeout(connect, delay);
          }
        };
        
        ws.onerror = (error) => {
          console.error('Fleet WebSocket error:', error);
          // Error is usually followed by close event, which handles reconnection
        };
      } catch (error) {
        console.error('Failed to create WebSocket:', error);
        
        // Schedule reconnection
        if (shouldReconnect) {
          reconnectAttempt++;
          const delay = calculateBackoffDelay(reconnectAttempt);
          reconnectTimeout = setTimeout(connect, delay);
        }
      }
    };
    
    // Initial connection
    connect();

    // Return cleanup function
    return () => {
      shouldReconnect = false;
      
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }
      
      if (ws) {
        ws.onclose = null; // Prevent reconnection on intentional close
        ws.close(1000, 'Client unsubscribed');
        ws = null;
      }
    };
  }
}

export const apiService = new ApiService();