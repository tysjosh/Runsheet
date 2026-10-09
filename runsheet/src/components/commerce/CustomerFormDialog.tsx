"use client";

/**
 * New customer (UI revamp task 3.3, design.md §5 "Customer": md FormDialog).
 * Posts to `POST /commerce/customers`; envelope field errors land inline.
 */
import { Field, FormDialog, INPUT_CLASS } from "@/components/ui";
import {
  type CreateCustomerPayload,
  type Customer,
  createCustomer,
} from "../../services/commerceApi";

type Values = {
  display_name: string;
  legal_name: string;
  primary_email: string;
  tax_id: string;
};

const EMPTY_VALUES: Values = {
  display_name: "",
  legal_name: "",
  primary_email: "",
  tax_id: "",
};

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateCustomer(v: Values) {
  const errors: Record<string, string | undefined> = {};
  if (!v.display_name.trim()) errors.display_name = "Enter a name.";
  const email = v.primary_email.trim();
  if (email && !EMAIL.test(email))
    errors.primary_email = "Enter a valid email address.";
  return errors;
}

export default function CustomerFormDialog({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved?: (customer: Customer) => void;
}) {
  const submit = async (v: Values): Promise<Customer> => {
    const payload: CreateCustomerPayload = {
      display_name: v.display_name.trim(),
    };
    if (v.legal_name.trim()) payload.legal_name = v.legal_name.trim();
    if (v.primary_email.trim()) payload.primary_email = v.primary_email.trim();
    if (v.tax_id.trim()) payload.tax_id = v.tax_id.trim();
    const res = await createCustomer(payload);
    return res.data;
  };
  if (!open) return null;
  return (
    <FormDialog<Values, Customer>
      open
      size="md"
      title="New customer"
      submitLabel="Create customer"
      successMessage="Customer created"
      initialValues={EMPTY_VALUES}
      validate={validateCustomer}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Name" required error={errors.display_name}>
            <input
              id="customer-name"
              type="text"
              value={values.display_name}
              onChange={(e) => set("display_name", e.target.value)}
              placeholder="e.g. Acme Fuel Co."
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Legal name" error={errors.legal_name}>
            <input
              id="customer-legal-name"
              type="text"
              value={values.legal_name}
              onChange={(e) => set("legal_name", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Billing email" error={errors.primary_email} span={1}>
            <input
              id="customer-email"
              type="email"
              value={values.primary_email}
              onChange={(e) => set("primary_email", e.target.value)}
              placeholder="billing@example.com"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Tax ID" error={errors.tax_id} span={1}>
            <input
              id="customer-tax-id"
              type="text"
              value={values.tax_id}
              onChange={(e) => set("tax_id", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}
