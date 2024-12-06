import transformers
from datasets import load_dataset
import shutil
import os
import nltk
import evaluate
import numpy as np

from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    Seq2SeqTrainingArguments,
    Seq2SeqTrainer,
    DataCollatorForSeq2Seq,
)


def prepare_log_dir(args):
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    exp_dir = os.path.join(args.output_dir, args.exp_name)
    if not os.path.exists(exp_dir):
        os.makedirs(exp_dir)

    exp_dir_folder_ls = os.listdir(exp_dir)
    if not exp_dir_folder_ls:
        exp_log_dir = os.path.join(exp_dir, f"{0}")
        os.makedirs(exp_log_dir)
    else:
        ls = []
        for i in range(len(exp_dir_folder_ls)):
            try:
                ls.append(int(exp_dir_folder_ls[i]))
            except:
                continue
        exp_dir_folder_ls = ls
        exp_dir_folder_ls.sort()
        exp_log_dir = os.path.join(exp_dir, f"{int(exp_dir_folder_ls[-1]) + 1}")
        os.makedirs(exp_log_dir)

    config_file_path = args.config
    shutil.copy(config_file_path, os.path.join(exp_log_dir, "config_BART.yml"))
    return exp_log_dir


def init_experiment(args, config, exp_type="train"):
    exp_log_dir = prepare_log_dir(args)
    args.output_dir = exp_log_dir

    for arg, value in vars(args).items():
        setattr(config, arg, value)

    if exp_type == "train":
        print(f"Saving log files to dir: {config.output_dir}")

    print("\n=========================================")
    print("Experiment Settings:")
    string = ""
    for arg, value in vars(config).items():
        string += f"({arg}: {value}) ; "
    print(string[0:-2])
    print("=========================================\n")
    return config


def load_model_tokenizer(model_name):
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    return model, tokenizer


def load_data(dataset_name="achrafothman/aslg_pc12"):
    data = load_dataset(dataset_name)["train"]

    train_test_split = data.train_test_split(
        test_size=0.1, seed=40
    )  # 90% train, 10% test
    test_data = train_test_split["test"]
    train_data = train_test_split["train"]

    train_val_split = train_data.train_test_split(
        test_size=0.1, seed=40
    )  # 10% of train for validation
    train_data = train_val_split["train"]
    val_data = train_val_split["test"]

    print("Train columns:", train_data.column_names)
    print("Total length of train data:", len(train_data))
    print("Total length of validation data:", len(val_data))
    print("Total length of test data:", len(test_data))

    print("\nSample data from train:")
    for i in range(3):
        print("Gloss: " + train_data["gloss"][i] + "Text: " + train_data["text"][i])

    return train_data, val_data, test_data


def batch_tokenize_preprocess(batch, tokenizer, max_source_length, max_target_length):
    source, target = batch["gloss"], batch["text"]
    source_tokenized = tokenizer(
        source, padding="max_length", truncation=True, max_length=max_source_length
    )
    target_tokenized = tokenizer(
        target, padding="max_length", truncation=True, max_length=max_target_length
    )

    batch = {k: v for k, v in source_tokenized.items()}

    # Ignore padding in the loss
    batch["labels"] = [
        [-100 if token == tokenizer.pad_token_id else token for token in l]
        for l in target_tokenized["input_ids"]
    ]
    return batch


def postprocess_text(preds, labels):
    preds = [pred.strip() for pred in preds]
    labels = [label.strip() for label in labels]

    # rougeLSum expects newline after each sentence
    preds = ["\n".join(nltk.sent_tokenize(pred)) for pred in preds]
    labels = ["\n".join(nltk.sent_tokenize(label)) for label in labels]

    return preds, labels


def make_compute_metrics(tokenizer):
    def compute_metrics(eval_preds):
        metric_rogue = evaluate.load("rouge")
        metric_bleu = evaluate.load("bleu")

        preds, labels = eval_preds
        if isinstance(preds, tuple):
            preds = preds[0]
        decoded_preds = tokenizer.batch_decode(preds, skip_special_tokens=True)
        # Replace -100 in the labels as we can't decode them.
        labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
        decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)

        # Some simple post-processing
        decoded_preds, decoded_labels = postprocess_text(decoded_preds, decoded_labels)

        result = metric_rogue.compute(
            predictions=decoded_preds, references=decoded_labels, use_stemmer=True
        )

        prediction_lens = [
            np.count_nonzero(pred != tokenizer.pad_token_id) for pred in preds
        ]
        result["gen_len"] = np.mean(prediction_lens)

        # Compute BLEU
        preds_tokens = [pred.split() for pred in decoded_preds]
        labels_tokens = [[label.split()] for label in decoded_labels]
        preds_sentences = [" ".join(tokens) for tokens in preds_tokens]
        references_sentences = [
            [" ".join(ref) for ref in refs] for refs in labels_tokens
        ]

        bleu_results = metric_bleu.compute(
            predictions=preds_sentences, references=references_sentences
        )["bleu"]
        result["bleu"] = bleu_results
        result = {k: round(v, 4) for k, v in result.items()}
        return result

    return compute_metrics


def generate_rich_text(
    test_data, model, tokenizer, encoder_max_length, compute_metrics=True
):
    inputs = tokenizer(
        test_data["gloss"],
        padding="max_length",
        truncation=True,
        max_length=encoder_max_length,
        return_tensors="pt",
    )
    input_ids = inputs.input_ids.to(model.device)
    attention_mask = inputs.attention_mask.to(model.device)
    outputs = model.generate(input_ids, attention_mask=attention_mask)
    output_str = tokenizer.batch_decode(outputs, skip_special_tokens=True)

    results = None
    if compute_metrics:
        preds, labels = postprocess_text(output_str, test_data["text"])

        rouge = evaluate.load("rouge")
        bleu = evaluate.load("bleu")

        # Compute ROUGE
        rouge_results = rouge.compute(
            predictions=preds, references=labels, use_stemmer=True
        )

        # Compute BLEU
        preds_tokens = [pred.split() for pred in preds]
        labels_tokens = [[label.split()] for label in labels]
        preds_sentences = [" ".join(tokens) for tokens in preds_tokens]
        references_sentences = [
            [" ".join(ref) for ref in refs] for refs in labels_tokens
        ]

        bleu_results = bleu.compute(
            predictions=preds_sentences, references=references_sentences
        )
        results = {"rouge": rouge_results, "bleu": bleu_results}
    return outputs, output_str, results
