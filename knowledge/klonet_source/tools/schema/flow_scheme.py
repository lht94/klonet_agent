scheme_pkt_gen1 = {
    "type": "object",
    "properties": {
        "src": { "type": "string" },
        "dst": { "type": "string" },
        "src_ip": { 
            "type": "string",
            "pattern": 
            "^(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|[1-9])\."
            "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)\."
            "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)\."
            "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)$"
        },
        "dst_ip": {
            "type": "string",
            "pattern": 
            "^(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|[1-9])\."
            "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)\."
            "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)\."
            "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)$"
        },
        "rate": { "type": "string" },
        "duration": { "type": "string" },
        "pkt_length": { "type": "string" },
        "dist": {
            "type": "string",
            "pattern": "(?:normal|exp)"
        },
        "normal_scale": { "type": "string" },
        "ip_tos": { "type": "string" },
        "ip_ttl": { "type": "string" },
        "ip_id": { "type": "string" },
        "proto": { "type": "string" },
        "tcp_header": { 
            "type": "object", 
            "properties": {
                "tcp_window": { "type": "string" },
                "sport": { "type": "string" },
                "dport": { "type": "string" }
            }
        },
        "udp_header": {
            "type": "object",
            "properties": {
                "sport": { "type": "string" },
                "dport": { "type": "string" }
            }
        }
    },
    "required": ["src", "dst", "src_ip", "dst_ip"]
}

scheme_pkt_gen2 = {
    "type": "object",
    "properties": {
        "src": { "type": "string" },
        "dst": { "type": "string" },
        "src_ip": { "type": "string" },
        "dst_ip": { "type": "string" },
        "rate": { "type": "string" },
        "pkt_length": { "type": "object" },
        "duration": { "type": "string" },
        "on_k": { "type": "string" },
        "on_min": { "type": "string" },
        "off_k": { "type": "string" },
        "off_min": { "type": "string" }
    },
    "required": ["src", "dst", "src_ip", "dst_ip"]
}

scheme_traffic_gen = {
    "type": "object",
    "properties": {
        "mode": { "type": "string" },
        "server_list": {
            "type": "array",
            "items": { "type": "string" }
        },
        "client": {
            "type": "object",
            "properties": {
                "client_name": { "type": "string" },
                "client_config": {
                    "type": "object",
                    "properties": {
                        "server_list": {
                            "type": "array",
                            "items": { "type": "string" }
                        },
                        "req_size_dist": { "type": "object" },
                        "dscp": { "type": "object" },
                        "rate": { "type": "object" },
                        "fanout": { "type": "object" }
                    }
                },
                "cli_param": { "type": "object" }
            }
        }
    },
    "required": ["server_list", "client_name"]
}