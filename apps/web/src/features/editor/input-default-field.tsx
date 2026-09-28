"use client";

import { useWatch } from "react-hook-form";

import { FileChooser } from "../files/file-chooser";
import { DefaultControl, type FieldRendererProps } from "./config-form";

/** An Input node's `default`: a file picker when the input type is "file", else the usual control. */
export function InputDefaultField(props: FieldRendererProps) {
  const inputType = useWatch({ control: props.form.control, name: "input_type" });
  if (inputType !== "file") return <DefaultControl {...props} />;
  return (
    <FileChooser
      id={props.inputId}
      value={typeof props.field.value === "string" ? props.field.value : undefined}
      onChange={(fileId) => props.field.onChange(fileId)}
      invalid={props.invalid}
      describedBy={props.describedBy}
      emptyLabel="No default: the run asks for a file"
      testId="input-default-file"
    />
  );
}
