st_link_post_schema = {
    "type": "object",
    "properties": {
        "linkchoice": {
            "type": "string",
            "enum":["static"]
        },
        "link": {
            "type": "string"
        },
        "ne": {
            "type": "string"
        },
        "bw_kbps": {
            "type": "integer",
            "minimum": 0 
        },
        "queue_size_bytes": {
            "type": "integer",
            "minimum": 0 
        },
        "delay_us": {
            "type": "integer",
            "minimum": 0 
        },
        "loss": {
            "type": "number",
            "minimum": 0, 
            "maximum": 100,
        },
        "jitter_us": {
            "type": "integer",
            "minimum": 0 
        },
        "correlation": {
            "type": "integer",
            "minimum": 0 ,
            "maximum": 100,
        },
        "delay_distribution": {
            "type": "string",
            "enum":["uniform","normal","pareto","paretonomal"]
        }
    },
    "required": ["linkchoice","link","ne","bw_kbps","queue_size_bytes","delay_us",
                 "loss","jitter_us","correlation","delay_distribution"]
}

mm_link_post_schema = {
    "type": "object",
    "properties": {
        "linkchoice": {
            "type": "string",
            "enum":["mmwave"]
        },
        "link": {
            "type": "string"
        },
        "ne": {
            "type": "string"
        },
        "link_scenario": {
            "type": "string",
            "enum":["lb","mobb","sb","sl"]
        },
        "queue_type": {
            "type": "string",
            "enum":["largefifo","fq_codel","pie","smallfifo"]
        },
        "loss": {
            "type": "number",
            "minimum": 0, 
            "maximum": 100,
        },
        "bandwidth_scaling": {
            "type": "number",
            "exclusiveMinimum": 0, 
        },
    },
    "required": ["linkchoice","link","ne","link_scenario","queue_type",
                 "loss","bandwidth_scaling"]
}

top_link_post_schem = {
    "type": "object",
    "properties": {
        "user": {
            "type": "string"
        },
        "topo": {
            "type": "string"
        },
        "links": {
            "type": "array",
            "minItems": 1,
            "items":{
                "type":"object",
                "properties":{
                    "linkchoice": {
                        "type": "string",
                        "enum":["mmwave","static"]
                    },
                    "link": {
                        "type": "string"
                    },
                    "ne": {
                        "type": "string"
                    },
                },
                "required":["linkchoice","link","ne"],
            },
        },
    },
    "required": ["user","topo","links"]
}