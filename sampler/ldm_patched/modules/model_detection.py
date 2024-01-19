import ldm_patched.modules.supported_models
import ldm_patched.modules.supported_models_base

def count_blocks(state_dict_keys, prefix_string):
    count = 0
    while True:
        c = False
        for k in state_dict_keys:
            if k.startswith(prefix_string.format(count)):
                c = True
                break
        if c == False:
            break
        count += 1
    return count

def calculate_transformer_depth(prefix, state_dict_keys, state_dict):
    context_dim = None
    use_linear_in_transformer = False

    transformer_prefix = prefix + "1.transformer_blocks."
    transformer_keys = sorted(list(filter(lambda a: a.startswith(transformer_prefix), state_dict_keys)))
    if len(transformer_keys) > 0:
        last_transformer_depth = count_blocks(state_dict_keys, transformer_prefix + '{}')
        context_dim = state_dict['{}0.attn2.to_k.weight'.format(transformer_prefix)].shape[1]
        use_linear_in_transformer = len(state_dict['{}1.proj_in.weight'.format(prefix)].shape) == 2
        time_stack = '{}1.time_stack.0.attn1.to_q.weight'.format(prefix) in state_dict or '{}1.time_mix_blocks.0.attn1.to_q.weight'.format(prefix) in state_dict
        return last_transformer_depth, context_dim, use_linear_in_transformer, time_stack
    return None

def detect_unet_config(state_dict, key_prefix, dtype):
    state_dict_keys = list(state_dict.keys())

    unet_config = {
        "use_checkpoint": False,
        "image_size": 32,
        "out_channels": 4,
        "use_spatial_transformer": True,
        "legacy": False
    }

    y_input = '{}label_emb.0.0.weight'.format(key_prefix)
    if y_input in state_dict_keys:
        unet_config["num_classes"] = "sequential"
        unet_config["adm_in_channels"] = state_dict[y_input].shape[1]
    else:
        unet_config["adm_in_channels"] = None

    unet_config["dtype"] = dtype
    model_channels = state_dict['{}input_blocks.0.0.weight'.format(key_prefix)].shape[0]
    in_channels = state_dict['{}input_blocks.0.0.weight'.format(key_prefix)].shape[1]

    num_res_blocks = []
    channel_mult = []
    attention_resolutions = []
    transformer_depth = []
    transformer_depth_output = []
    context_dim = None
    use_linear_in_transformer = False

    video_model = False

    current_res = 1
    count = 0

    last_res_blocks = 0
    last_channel_mult = 0

    input_block_count = count_blocks(state_dict_keys, '{}input_blocks'.format(key_prefix) + '.{}.')
    for count in range(input_block_count):
        prefix = '{}input_blocks.{}.'.format(key_prefix, count)
        prefix_output = '{}output_blocks.{}.'.format(key_prefix, input_block_count - count - 1)

        block_keys = sorted(list(filter(lambda a: a.startswith(prefix), state_dict_keys)))
        if len(block_keys) == 0:
            break

        block_keys_output = sorted(list(filter(lambda a: a.startswith(prefix_output), state_dict_keys)))

        if "{}0.op.weight".format(prefix) in block_keys: #new layer
            num_res_blocks.append(last_res_blocks)
            channel_mult.append(last_channel_mult)

            current_res *= 2
            last_res_blocks = 0
            last_channel_mult = 0
            out = calculate_transformer_depth(prefix_output, state_dict_keys, state_dict)
            if out is not None:
                transformer_depth_output.append(out[0])
            else:
                transformer_depth_output.append(0)
        else:
            res_block_prefix = "{}0.in_layers.0.weight".format(prefix)
            if res_block_prefix in block_keys:
                last_res_blocks += 1
                last_channel_mult = state_dict["{}0.out_layers.3.weight".format(prefix)].shape[0] // model_channels

                out = calculate_transformer_depth(prefix, state_dict_keys, state_dict)
                if out is not None:
                    transformer_depth.append(out[0])
                    if context_dim is None:
                        context_dim = out[1]
                        use_linear_in_transformer = out[2]
                        video_model = out[3]
                else:
                    transformer_depth.append(0)

            res_block_prefix = "{}0.in_layers.0.weight".format(prefix_output)
            if res_block_prefix in block_keys_output:
                out = calculate_transformer_depth(prefix_output, state_dict_keys, state_dict)
                if out is not None:
                    transformer_depth_output.append(out[0])
                else:
                    transformer_depth_output.append(0)


    num_res_blocks.append(last_res_blocks)
    channel_mult.append(last_channel_mult)
    if "{}middle_block.1.proj_in.weight".format(key_prefix) in state_dict_keys:
        transformer_depth_middle = count_blocks(state_dict_keys, '{}middle_block.1.transformer_blocks.'.format(key_prefix) + '{}')
    else:
        transformer_depth_middle = -1

    unet_config["in_channels"] = in_channels
    unet_config["model_channels"] = model_channels
    unet_config["num_res_blocks"] = num_res_blocks
    unet_config["transformer_depth"] = transformer_depth
    unet_config["transformer_depth_output"] = transformer_depth_output
    unet_config["channel_mult"] = channel_mult
    unet_config["transformer_depth_middle"] = transformer_depth_middle
    unet_config['use_linear_in_transformer'] = use_linear_in_transformer
    unet_config["context_dim"] = context_dim

    if video_model:
        unet_config["extra_ff_mix_layer"] = True
        unet_config["use_spatial_context"] = True
        unet_config["merge_strategy"] = "learned_with_images"
        unet_config["merge_factor"] = 0.0
        unet_config["video_kernel_size"] = [3, 1, 1]
        unet_config["use_temporal_resblock"] = True
        unet_config["use_temporal_attention"] = True
    else:
        unet_config["use_temporal_resblock"] = False
        unet_config["use_temporal_attention"] = False

    return unet_config

def model_config_from_unet_config(unet_config):
    for model_config in ldm_patched.modules.supported_models.models:
        if model_config.matches(unet_config):
            return model_config(unet_config)

    print("no match", unet_config)
    return None

def model_config_from_unet(state_dict, unet_key_prefix, dtype, use_base_if_no_match=False):
    unet_config = detect_unet_config(state_dict, unet_key_prefix, dtype)
    model_config = model_config_from_unet_config(unet_config)
    if model_config is None and use_base_if_no_match:
        return ldm_patched.modules.supported_models_base.BASE(unet_config)
    else:
        return model_config

def convert_config(unet_config):
    new_config = unet_config.copy()
    num_res_blocks = new_config.get("num_res_blocks", None)
    channel_mult = new_config.get("channel_mult", None)

    if isinstance(num_res_blocks, int):
        num_res_blocks = len(channel_mult) * [num_res_blocks]

    if "attention_resolutions" in new_config:
        attention_resolutions = new_config.pop("attention_resolutions")
        transformer_depth = new_config.get("transformer_depth", None)
        transformer_depth_middle = new_config.get("transformer_depth_middle", None)

        if isinstance(transformer_depth, int):
            transformer_depth = len(channel_mult) * [transformer_depth]
        if transformer_depth_middle is None:
            transformer_depth_middle =  transformer_depth[-1]
        t_in = []
        t_out = []
        s = 1
        for i in range(len(num_res_blocks)):
            res = num_res_blocks[i]
            d = 0
            if s in attention_resolutions:
                d = transformer_depth[i]

            t_in += [d] * res
            t_out += [d] * (res + 1)
            s *= 2
        transformer_depth = t_in
        transformer_depth_output = t_out
        new_config["transformer_depth"] = t_in
        new_config["transformer_depth_output"] = t_out
        new_config["transformer_depth_middle"] = transformer_depth_middle

    new_config["num_res_blocks"] = num_res_blocks
    return new_config


def unet_config_from_diffusers_unet(state_dict, dtype):
    match = {}
    transformer_depth = []

    attn_res = 1
    down_blocks = count_blocks(state_dict, "down_blocks.{}")
    for i in range(down_blocks):
        attn_blocks = count_blocks(state_dict, "down_blocks.{}.attentions.".format(i) + '{}')
        for ab in range(attn_blocks):
            transformer_count = count_blocks(state_dict, "down_blocks.{}.attentions.{}.transformer_blocks.".format(i, ab) + '{}')
            transformer_depth.append(transformer_count)
            if transformer_count > 0:
                match["context_dim"] = state_dict["down_blocks.{}.attentions.{}.transformer_blocks.0.attn2.to_k.weight".format(i, ab)].shape[1]

        attn_res *= 2
        if attn_blocks == 0:
            transformer_depth.append(0)
            transformer_depth.append(0)

    match["transformer_depth"] = transformer_depth

    match["model_channels"] = state_dict["conv_in.weight"].shape[0]
    match["in_channels"] = state_dict["conv_in.weight"].shape[1]
    match["adm_in_channels"] = None
    if "class_embedding.linear_1.weight" in state_dict:
        match["adm_in_channels"] = state_dict["class_embedding.linear_1.weight"].shape[1]
    elif "add_embedding.linear_1.weight" in state_dict:
        match["adm_in_channels"] = state_dict["add_embedding.linear_1.weight"].shape[1]

    SDXL = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
            'num_classes': 'sequential', 'adm_in_channels': 2816, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
            'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 2, 2, 10, 10], 'channel_mult': [1, 2, 4], 'transformer_depth_middle': 10,
            'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64, 'transformer_depth_output': [0, 0, 0, 2, 2, 2, 10, 10, 10],
            'use_temporal_attention': False, 'use_temporal_resblock': False}

    SDXL_refiner = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
                    'num_classes': 'sequential', 'adm_in_channels': 2560, 'dtype': dtype, 'in_channels': 4, 'model_channels': 384,
                    'num_res_blocks': [2, 2, 2, 2], 'transformer_depth': [0, 0, 4, 4, 4, 4, 0, 0], 'channel_mult': [1, 2, 4, 4], 'transformer_depth_middle': 4,
                    'use_linear_in_transformer': True, 'context_dim': 1280, 'num_head_channels': 64, 'transformer_depth_output': [0, 0, 0, 4, 4, 4, 4, 4, 4, 0, 0, 0],
                    'use_temporal_attention': False, 'use_temporal_resblock': False}

    SD21 = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
            'adm_in_channels': None, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320, 'num_res_blocks': [2, 2, 2, 2],
            'transformer_depth': [1, 1, 1, 1, 1, 1, 0, 0], 'channel_mult': [1, 2, 4, 4], 'transformer_depth_middle': 1, 'use_linear_in_transformer': True,
            'context_dim': 1024, 'num_head_channels': 64, 'transformer_depth_output': [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
            'use_temporal_attention': False, 'use_temporal_resblock': False}

    SD21_uncliph = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
                    'num_classes': 'sequential', 'adm_in_channels': 2048, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
                    'num_res_blocks': [2, 2, 2, 2], 'transformer_depth': [1, 1, 1, 1, 1, 1, 0, 0], 'channel_mult': [1, 2, 4, 4], 'transformer_depth_middle': 1,
                    'use_linear_in_transformer': True, 'context_dim': 1024, 'num_head_channels': 64, 'transformer_depth_output': [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
                    'use_temporal_attention': False, 'use_temporal_resblock': False}

    SD21_unclipl = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
                    'num_classes': 'sequential', 'adm_in_channels': 1536, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
                    'num_res_blocks': [2, 2, 2, 2], 'transformer_depth': [1, 1, 1, 1, 1, 1, 0, 0], 'channel_mult': [1, 2, 4, 4], 'transformer_depth_middle': 1,
                    'use_linear_in_transformer': True, 'context_dim': 1024, 'num_head_channels': 64, 'transformer_depth_output': [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
                    'use_temporal_attention': False, 'use_temporal_resblock': False}

    SD15 = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False, 'adm_in_channels': None,
            'dtype': dtype, 'in_channels': 4, 'model_channels': 320, 'num_res_blocks': [2, 2, 2, 2], 'transformer_depth': [1, 1, 1, 1, 1, 1, 0, 0],
            'channel_mult': [1, 2, 4, 4], 'transformer_depth_middle': 1, 'use_linear_in_transformer': False, 'context_dim': 768, 'num_heads': 8,
            'transformer_depth_output': [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
            'use_temporal_attention': False, 'use_temporal_resblock': False}

    SDXL_mid_cnet = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
                     'num_classes': 'sequential', 'adm_in_channels': 2816, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
                     'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 0, 0, 1, 1], 'channel_mult': [1, 2, 4], 'transformer_depth_middle': 1,
                     'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64, 'transformer_depth_output': [0, 0, 0, 0, 0, 0, 1, 1, 1],
                     'use_temporal_attention': False, 'use_temporal_resblock': False}

    SDXL_small_cnet = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
                       'num_classes': 'sequential', 'adm_in_channels': 2816, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
                       'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 0, 0, 0, 0], 'channel_mult': [1, 2, 4], 'transformer_depth_middle': 0,
                       'use_linear_in_transformer': True, 'num_head_channels': 64, 'context_dim': 1, 'transformer_depth_output': [0, 0, 0, 0, 0, 0, 0, 0, 0],
                       'use_temporal_attention': False, 'use_temporal_resblock': False}

    SDXL_diffusers_inpaint = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
                              'num_classes': 'sequential', 'adm_in_channels': 2816, 'dtype': dtype, 'in_channels': 9, 'model_channels': 320,
                              'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 2, 2, 10, 10], 'channel_mult': [1, 2, 4], 'transformer_depth_middle': 10,
                              'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64, 'transformer_depth_output': [0, 0, 0, 2, 2, 2, 10, 10, 10],
                              'use_temporal_attention': False, 'use_temporal_resblock': False}

    SSD_1B = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
              'num_classes': 'sequential', 'adm_in_channels': 2816, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
              'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 2, 2, 4, 4], 'transformer_depth_output': [0, 0, 0, 1, 1, 2, 10, 4, 4],
              'channel_mult': [1, 2, 4], 'transformer_depth_middle': -1, 'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64,
              'use_temporal_attention': False, 'use_temporal_resblock': False}

    Segmind_Vega = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
              'num_classes': 'sequential', 'adm_in_channels': 2816, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
              'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 1, 1, 2, 2], 'transformer_depth_output': [0, 0, 0, 1, 1, 1, 2, 2, 2],
              'channel_mult': [1, 2, 4], 'transformer_depth_middle': -1, 'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64,
              'use_temporal_attention': False, 'use_temporal_resblock': False}

    supported_models = [SDXL, SDXL_refiner, SD21, SD15, SD21_uncliph, SD21_unclipl, SDXL_mid_cnet, SDXL_small_cnet, SDXL_diffusers_inpaint, SSD_1B, Segmind_Vega]

    for unet_config in supported_models:
        matches = True
        for k in match:
            if match[k] != unet_config[k]:
                matches = False
                break
        if matches:
            return convert_config(unet_config)
    return None

def model_config_from_diffusers_unet(state_dict, dtype):
    unet_config = unet_config_from_diffusers_unet(state_dict, dtype)
    if unet_config is not None:
        return model_config_from_unet_config(unet_config)
    return None
