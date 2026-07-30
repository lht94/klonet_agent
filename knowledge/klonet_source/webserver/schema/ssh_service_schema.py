schema_ssh_post = {
    "type": "object",
    "properties": {
        "user": {
            "type": "string"
        },
        "topo": {
            "type": "string"
        },
        "ne": {
            "type": "string"
        },
        "ssh": {
            "type": "boolean"
        },
        "passwd": {
            "type": "string"
        }
    },
    "required": [
        'user',
        'topo',
        'ne',
        'ssh',
        'passwd'
    ]
}

schema_ssh_get = {
    "type": "object",
    "properties": {
        "user": {
            "type": "string"
        },
        "topo": {
            "type": "string"
        },
        "ne": {
            "type": "string"
        }
    },
    "required": [
        'user',
        'topo',
        'ne'
    ]
}

schema_port_modify = {
    "type": "object",
    "properties": {
        "user": {
            "type": "string"
        },
        "topo": {
            "type": "string"
        },
        "ne": {
            "type": "string"
        },
        "port_mapping": {
            "type": "array",
            "items": {
                "type": "number"
            }
        }
    },
    "required": [
        'user',
        'topo',
        'ne'
    ]
}